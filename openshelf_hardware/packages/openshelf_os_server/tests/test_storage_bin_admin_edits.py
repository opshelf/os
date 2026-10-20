"""Admin bin-member edits: permissions, membership scrubbing on delete, the
no_compatible_bin refusal and direct fallback, and return checks that accept an admin edit."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from openshelf_os.common import OperatingMode
from openshelf_os.config.settings import StorageBinSettings
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import (
    AccessLevel,
    Item,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
)
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import storage as storage_router


def _user(role_name: str) -> SimpleNamespace:
    return SimpleNamespace(id=1, role=SimpleNamespace(name=role_name))


class FakeRuntime:
    def __init__(self) -> None:
        self.has_active_commands = AsyncMock(return_value=False)
        self.publish_status = AsyncMock()

    async def publish_event(self, payload: dict) -> None:
        pass

    def set_bin_io_box_occupied(self, occupied: bool) -> None:
        pass

    def is_bin_io_box_occupied(self) -> bool:
        return False


def _mock_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.mock)
    monkeypatch.setattr(storage_router, "get_mock_robot_runtime", lambda: FakeRuntime())


async def _ensure_level(db, level_id: int) -> None:
    level = (await db.execute(select(AccessLevel).where(AccessLevel.id == level_id))).scalar_one_or_none()
    if level is None:
        db.add(AccessLevel(id=level_id, name=f"level-{level_id}"))
        await db.flush()


async def _setup_members(db, barcode: str, count: int, access_level_id: int) -> list:
    await _ensure_level(db, access_level_id)
    storage = StorageService(db)
    items = [
        await storage.create_item(
            f"1234567892{index}", f"Member {index}",
            {"width": 100, "height": 100, "length": 60},
            access_level_id=access_level_id,
        )
        for index in range(count)
    ]
    row = StorageBin(
        barcode=barcode,
        side="front_module",
        shelf_idx=0,
        cell_idx=0,
        inner_dimensions={"width": 200, "height": 100, "length": 100},
        max_items=6,
        status="at_home",
        active=True,
    )
    db.add(row)
    await db.flush()
    for index, item in enumerate(items):
        db.add(StorageBinItem(bin_id=row.id, item_id=item.id))
    await db.commit()
    return items


async def _bin_outside(monkeypatch, db) -> StorageBin:
    configured = StorageBinSettings.model_validate({
        "barcode": "900000000303",
        "home_location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        "inner_dimensions": {"width": 200, "height": 100, "length": 100},
        "max_items": 6,
    })
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: SimpleNamespace(storage=SimpleNamespace(bins=[configured])))
    await bin_storage.BinStorageService(db).sync_config()
    row = (await db.execute(select(StorageBin).where(StorageBin.active.is_(True)))).scalars().one()
    row.status = "outside"
    await db.commit()
    return row


async def _only_bin_outside(monkeypatch, db) -> Item:
    await _bin_outside(monkeypatch, db)
    return await StorageService(db).create_item("123456789303", "Fits", {"width": 50, "height": 50, "length": 50})


async def test_bin_store_with_no_compatible_bin_reports_the_cause(monkeypatch):
    _mock_mode(monkeypatch)
    async with AsyncSessionLocal() as db:
        item = await _only_bin_outside(monkeypatch, db)
        request = storage_router.BinWorkflowCreate(
            operation="store", barcode=item.barcode, storage_mode="bin"
        )
        with pytest.raises(HTTPException) as error:
            await storage_router.create_bin_workflow(request, session=db, current_user=_user("admin"))
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "no_compatible_bin"
        assert error.value.detail["message"] == "Every bin that fits this item is outside the machine."
        assert (await db.execute(select(StorageBinWorkflow))).scalars().all() == []


async def test_store_without_bin_mode_falls_back_to_direct_when_no_bin_fits(monkeypatch):
    _mock_mode(monkeypatch)
    async with AsyncSessionLocal() as db:
        item = await _only_bin_outside(monkeypatch, db)
        request = storage_router.BinWorkflowCreate(operation="store", barcode=item.barcode)
        result = await storage_router.create_bin_workflow(request, session=db, current_user=_user("admin"))
        assert result == {"storage_mode": "direct", "workflow": None}
        assert (await db.execute(select(StorageBinWorkflow))).scalars().all() == []


async def test_bin_store_says_an_unmeasured_item_needs_its_size(monkeypatch):
    # A record without dimensions fits no bin, which must be reported as a missing size
    # rather than a footprint too large for every compartment.
    _mock_mode(monkeypatch)
    async with AsyncSessionLocal() as db:
        await _bin_outside(monkeypatch, db)
        item = Item(barcode="123456789304", name="Unmeasured", dimensions=None)
        db.add(item)
        await db.commit()

        request = storage_router.BinWorkflowCreate(
            operation="store", barcode=item.barcode, storage_mode="bin"
        )
        with pytest.raises(HTTPException) as error:
            await storage_router.create_bin_workflow(request, session=db, current_user=_user("admin"))
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "no_compatible_bin"
        assert error.value.detail["message"] == (
            "The item needs its size measured before it can go in a bin."
        )

        fallback = storage_router.BinWorkflowCreate(operation="store", barcode=item.barcode)
        result = await storage_router.create_bin_workflow(
            fallback, session=db, current_user=_user("admin")
        )
        assert result == {"storage_mode": "direct", "workflow": None}
        assert (await db.execute(select(StorageBinWorkflow))).scalars().all() == []


async def test_admin_edits_a_bin_member_while_the_bin_is_outside():
    async with AsyncSessionLocal() as db:
        await _ensure_level(db, 2)
        await _setup_members(db, "900000000310", 2, access_level_id=1)
        service = bin_storage.BinStorageService(db)
        member = (
            await db.execute(
                select(StorageBinItem)
                .join(StorageBin)
                .where(StorageBin.barcode == "900000000310")
            )
        ).scalars().all()
        taken = await service.begin("retrieve", await db.get(Item, member[0].item_id), None)
        await service.transition(taken, "awaiting_operator")
        await service.close(taken)
        remaining = await db.get(Item, member[1].item_id)

        denied = None
        try:
            await storage_router.update_item(
                storage_router.ItemUpdateRequest(
                    barcode=remaining.barcode,
                    dimensions={"width": 80, "height": 80, "length": 80},
                ),
                session=db,
                current_user=_user("operator"),
            )
        except Exception as exc:
            denied = exc
        assert denied is not None and denied.status_code == 403
        assert denied.detail["code"] == "bin_item_edit_requires_admin"
        assert denied.detail["message"] == (
            "Only an admin can change the size or access level of an item in a bin."
        )

        await storage_router.update_item(
            storage_router.ItemUpdateRequest(
                barcode=remaining.barcode, access_level_id=2
            ),
            session=db,
            current_user=_user("admin"),
        )
        await db.refresh(remaining)
        assert remaining.access_level_id == 2

        await storage_router.update_item(
            storage_router.ItemUpdateRequest(
                barcode=remaining.barcode,
                dimensions={"width": 90, "height": 90, "length": 90},
            ),
            session=db,
            current_user=_user("admin"),
        )
        await db.refresh(remaining)
        assert remaining.dimensions == {
            "width": 90,
            "height": 90,
            "length": 90,
        }


async def test_admin_cannot_resize_a_member_while_its_bin_returns():
    async with AsyncSessionLocal() as db:
        items = await _setup_members(db, "900000000315", 2, access_level_id=1)
        service = bin_storage.BinStorageService(db)
        moving = await service.begin("retrieve", items[1], None)
        moving.state = "returning_bin"
        await db.commit()

        with pytest.raises(HTTPException) as error:
            await storage_router.update_item(
                storage_router.ItemUpdateRequest(
                    barcode=items[0].barcode,
                    dimensions={"width": 150, "height": 100, "length": 100},
                ),
                session=db,
                current_user=_user("admin"),
            )
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "bin_workflow_conflict"
        assert error.value.detail["message"] == "Wait for the bin to finish moving."
        await db.refresh(items[0])
        assert items[0].dimensions == {"width": 100, "height": 100, "length": 60}


async def test_admin_delete_of_a_bin_member_removes_its_membership():
    async with AsyncSessionLocal() as db:
        items = await _setup_members(db, "900000000320", 2, access_level_id=1)

        denied = None
        try:
            await storage_router.delete_item(
                storage_router.BarcodeRequest(barcode=items[0].barcode),
                session=db,
                current_user=_user("operator"),
            )
        except Exception as exc:
            denied = exc
        assert denied is not None and denied.status_code == 403
        assert denied.detail["code"] == "bin_item_delete_requires_admin"
        assert denied.detail["message"] == (
            "Only an admin can delete an item that is in a bin."
        )

        await storage_router.delete_item(
            storage_router.BarcodeRequest(barcode=items[0].barcode),
            session=db,
            current_user=_user("admin"),
        )

        assert await db.get(Item, items[0].id) is None
        assert (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == items[0].id))
        ).scalars().all() == []
        assert (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == items[1].id))
        ).scalars().all() != []


async def test_an_item_placed_by_a_running_import_blocks_an_admin_delete():
    # Deleting it would free a compartment the next import placement reuses while the item
    # is still physically in it.
    async with AsyncSessionLocal() as db:
        await _setup_members(db, "900000000340", 0, access_level_id=1)
        tray = await db.scalar(select(StorageBin).where(StorageBin.barcode == "900000000340"))
        placed = await StorageService(db).create_item(
            "123456789341", "Imported", {"width": 50, "height": 50, "length": 50},
            access_level_id=1,
        )
        service = bin_storage.BinStorageService(db)
        workflow = await service.begin_batch(tray, None, "import-1")
        await service.transition(workflow, "awaiting_operator")
        await service.place_in_batch(workflow, placed)

        with pytest.raises(HTTPException) as blocked:
            await storage_router.delete_item(
                storage_router.BarcodeRequest(barcode=placed.barcode),
                session=db,
                current_user=_user("admin"),
            )

        assert blocked.value.status_code == 409
        assert blocked.value.detail["code"] == "bin_workflow_conflict"
        assert await db.get(Item, placed.id) is not None


async def test_a_pre_existing_member_blocks_an_admin_delete_while_an_import_fills_the_bin():
    # Freeing its compartment would hand the next placed unit the slot the deleted item
    # still physically occupies.
    async with AsyncSessionLocal() as db:
        await _ensure_level(db, 1)
        storage = StorageService(db)
        member = await storage.create_item(
            "123456789350", "Member", {"width": 50, "height": 50, "length": 50},
            access_level_id=1,
        )
        tray = StorageBin(
            barcode="900000000350",
            side="front_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions={"width": 200, "height": 100, "length": 100},
            compartment_layout={"rows": 1, "columns": 3},
            max_items=3,
            status="at_home",
            active=True,
        )
        db.add(tray)
        await db.flush()
        db.add(StorageBinItem(bin_id=tray.id, item_id=member.id, compartment=1))
        await db.commit()

        service = bin_storage.BinStorageService(db)
        workflow = await service.begin_batch(tray, None, "import-1")
        await service.transition(workflow, "awaiting_operator")
        first = await storage.create_item(
            "123456789351", "First", {"width": 50, "height": 50, "length": 50},
            access_level_id=1,
        )
        assert await service.place_in_batch(workflow, first) == 2

        with pytest.raises(HTTPException) as blocked:
            await storage_router.delete_item(
                storage_router.BarcodeRequest(barcode=member.barcode),
                session=db,
                current_user=_user("admin"),
            )

        assert blocked.value.status_code == 409
        assert blocked.value.detail["code"] == "bin_workflow_conflict"
        assert await db.get(Item, member.id) is not None

        second = await storage.create_item(
            "123456789352", "Second", {"width": 50, "height": 50, "length": 50},
            access_level_id=1,
        )
        assert await service.place_in_batch(workflow, second) == 3
        assert dict(
            (
                await db.execute(
                    select(StorageBinItem.item_id, StorageBinItem.compartment).where(
                        StorageBinItem.bin_id == tray.id
                    )
                )
            ).all()
        ) == {member.id: 1, first.id: 2, second.id: 3}


async def test_admin_edited_member_does_not_block_the_bins_return():
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        bin_row = StorageBin(
            barcode="900000000340",
            side="front_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions={"width": 200, "height": 100, "length": 100},
            max_items=6,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await _ensure_level(db, 1)
        await _ensure_level(db, 2)
        await db.commit()
        for index in range(2):
            member = await storage.create_item(
                f"12345678934{index}", f"Member {index}",
                {"width": 100, "height": 100, "length": 60},
                access_level_id=1,
            )
            db.add(StorageBinItem(bin_id=bin_row.id, item_id=member.id))
        await db.commit()

        service = bin_storage.BinStorageService(db)
        incoming = await storage.create_item(
            "123456789342", "Incoming",
            {"width": 100, "height": 100, "length": 60},
            access_level_id=1,
        )
        stored = await service.begin("store", incoming, None)
        assert stored is not None
        await service.transition(stored, "awaiting_operator")
        await service.close(stored)

        # An admin resizes the member and moves its access level while the bin is out;
        # the bin's contents now exceed the volume a fresh check would allow.
        await storage_router.update_item(
            storage_router.ItemUpdateRequest(
                barcode=incoming.barcode,
                dimensions={"width": 150, "height": 100, "length": 100},
                access_level_id=2,
            ),
            session=db,
            current_user=_user("admin"),
        )
        member_ids = (
            await db.execute(select(StorageBinItem.item_id).where(StorageBinItem.bin_id == bin_row.id))
        ).scalars().all()
        members = [await db.get(Item, member_id) for member_id in member_ids]
        total_volume = sum(
            member.dimensions["width"]
            * member.dimensions["height"]
            * member.dimensions["length"]
            for member in members
        )
        assert total_volume > 2_000_000

        returned = await service.begin_return("900000000340")
        confirmed = (
            await db.execute(select(StorageBinItem.item_id).where(StorageBinItem.bin_id == bin_row.id))
        ).scalars().all()
        assert await service.prepare_return(returned, list(confirmed)) is True
        await service.complete_return(returned)

        await db.refresh(bin_row)
        assert bin_row.status == "at_home"
        assert returned.state == "completed"


async def test_admin_resized_members_do_not_block_an_item_put_back():
    async with AsyncSessionLocal() as db:
        items = await _setup_members(db, "900000000345", 3, access_level_id=1)
        bin_row = await db.scalar(select(StorageBin).where(StorageBin.barcode == "900000000345"))
        service = bin_storage.BinStorageService(db)
        taken_out = await service.begin("retrieve", items[2], None)
        await service.transition(taken_out, "awaiting_operator")
        await service.close(taken_out)

        # While the bin is out an admin resizes the members it still holds past its capacity.
        # The item taken out goes back in, so only it is checked against the bin.
        for member in items[:2]:
            await storage_router.update_item(
                storage_router.ItemUpdateRequest(
                    barcode=member.barcode,
                    dimensions={"width": 200, "height": 100, "length": 100},
                ),
                session=db,
                current_user=_user("admin"),
            )
        member_ids = (
            await db.execute(
                select(StorageBinItem.item_id).where(StorageBinItem.bin_id == bin_row.id)
            )
        ).scalars().all()
        members = [await db.get(Item, member_id) for member_id in member_ids]
        member_volume = sum(
            member.dimensions["width"]
            * member.dimensions["height"]
            * member.dimensions["length"]
            for member in members
        )
        assert member_volume > 2_000_000

        returned = await service.begin_return("900000000345")
        assert await service.prepare_return(returned, [*member_ids, items[2].id]) is True
        await service.complete_return(returned)

        await db.refresh(bin_row)
        assert bin_row.status == "at_home"
        assert returned.state == "completed"
        assert (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == items[2].id))
        ).scalars().all() != []


async def test_an_item_put_back_must_match_the_members_access_level():
    async with AsyncSessionLocal() as db:
        await _ensure_level(db, 2)
        items = await _setup_members(db, "900000000346", 3, access_level_id=1)
        service = bin_storage.BinStorageService(db)
        taken_out = await service.begin("retrieve", items[2], None)
        await service.transition(taken_out, "awaiting_operator")
        await service.close(taken_out)
        await storage_router.update_item(
            storage_router.ItemUpdateRequest(barcode=items[2].barcode, access_level_id=2),
            session=db,
            current_user=_user("admin"),
        )
        await db.refresh(items[2])
        assert items[2].access_level_id == 2

        returned = await service.begin_return("900000000346")
        with pytest.raises(bin_storage.BinWorkflowConflict) as refused:
            await service.prepare_return(returned, [item.id for item in items])
        assert str(refused.value) == "The confirmed items have different access levels."


async def test_bin_retrieve_names_the_items_the_user_cannot_access(monkeypatch):
    _mock_mode(monkeypatch)
    async with AsyncSessionLocal() as db:
        members = await _setup_members(db, "900000000350", 2, access_level_id=1)
        user = SimpleNamespace(
            id=1, role=SimpleNamespace(name="operator"), access_level_id=None, is_system=False
        )
        request = storage_router.BinWorkflowCreate(operation="retrieve", barcode=members[0].barcode)
        with pytest.raises(HTTPException) as error:
            await storage_router.create_bin_workflow(request, session=db, current_user=user)
        assert error.value.status_code == 403
        assert error.value.detail["code"] == "bin_access_denied"
        assert "Member 0, Member 1" in error.value.detail["message"]
