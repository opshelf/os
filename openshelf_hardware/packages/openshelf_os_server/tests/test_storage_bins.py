import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.robot.commands import StoreItemCommand
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core.bin_storage import BinStorageService, BinWorkflowConflict
from openshelf_os_server.core.mock_robot import MockRobotRuntime
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import (
    AccessLevel,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
)
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router
from openshelf_os_server.routers import storage as storage_router


class FakeWorker:
    def __init__(self) -> None:
        self.commands = []
        self.events = []
        self.is_needs_home = False
        self._is_emergency_stopped = False
        self._is_crashed = False

    async def enqueue_command(self, command) -> None:
        self.commands.append(command)

    async def publish_event(self, event) -> None:
        self.events.append(event)

    def get_cached_scale_status(self):
        return SimpleNamespace(occupied=True)

    def needs_home(self) -> bool:
        return self.is_needs_home

    def needs_home_reason(self) -> str | None:
        return "restart requires home" if self.is_needs_home else None


class BlockingWorker(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self._current_command = None
        self._command_queue = SimpleNamespace(qsize=lambda: len(self.commands))
        self.command_enqueue_started = asyncio.Event()
        self.release_command_enqueue = asyncio.Event()

    async def enqueue_command(self, command) -> None:
        self.command_enqueue_started.set()
        await self.release_command_enqueue.wait()
        self.commands.append(command)

    def needs_home(self) -> bool:
        return False

    def get_cached_scale_status(self):
        return SimpleNamespace(occupied=False)


def _bin_config(*entries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(storage=SimpleNamespace(bins=list(entries)))


def _configured_bin(barcode: str, side: str, shelf_idx: int, cell_idx: int) -> SimpleNamespace:
    return SimpleNamespace(
        barcode=barcode,
        home_location=SimpleNamespace(side=side, shelf_idx=shelf_idx, cell_idx=cell_idx),
    )


def _workflow_bin(barcode: str, side: str, shelf_idx: int, cell_idx: int) -> SimpleNamespace:
    bin_row = _configured_bin(barcode, side, shelf_idx, cell_idx)
    bin_row.inner_dimensions = SimpleNamespace(
        model_dump=lambda mode: {"width": 30, "height": 30, "length": 30}
    )
    bin_row.max_items = 2
    return bin_row


@pytest.mark.anyio
async def test_direct_store_rejects_live_bin_barcode_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _configured_bin("900000000010", "left_module", 0, 0)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config(configured))
    tray_home = {"side": "left_module", "shelf_idx": 0, "cell_idx": 0}

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as barcode_error:
            await robot_router._validate_direct_store_reservation({"upca": configured.barcode}, db)
        assert barcode_error.value.status_code == 409
        assert barcode_error.value.detail["code"] == "reserved_bin_barcode"

        # A tray home is a usable target for the reservation guard in both modes: the tray's
        # return reports the occupied cell instead of the store being refused up front. The
        # cell-level check below still refuses it, as production's maintenance status does.
        with pytest.raises(HTTPException) as home_error:
            await robot_router._validate_mock_store(
                StoreItemCommand(
                    item={
                        "upca": "123456789012",
                        "name": "Direct item",
                        "dimensions": {"width": 1, "height": 1, "length": 1},
                        "location": tray_home,
                    }
                ),
                db,
            )
        assert home_error.value.status_code == 409
        assert home_error.value.detail["code"] == "cell_unavailable"

        await robot_router._validate_direct_store_reservation(
            {"upca": "123456789013", "location": tray_home}, db
        )

        with pytest.raises(HTTPException) as malformed_mock_error:
            await robot_router._validate_mock_store(
                StoreItemCommand(
                    item={
                        "upca": "123456789015",
                        "name": "Direct item",
                        "dimensions": {"width": 1, "height": 1, "length": 1},
                        "location": {"side": "left_module", "shelf_idx": False, "cell_idx": 0},
                    }
                ),
                db,
            )
        assert malformed_mock_error.value.status_code == 422
        assert malformed_mock_error.value.detail["code"] == "invalid_location"

        worker = MagicMock()
        worker.needs_home.return_value = False
        monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
        monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
        with pytest.raises(HTTPException) as malformed_hardware_error:
            await robot_router.robot_commands(
                StoreItemCommand(
                    location={"side": "left_module", "shelf_idx": "0", "cell_idx": 0},
                    item={
                        "upca": "123456789016",
                        "name": "Direct item",
                        "dimensions": {"width": 1, "height": 1, "length": 1},
                    },
                ),
                worker=worker,
                current_user=SimpleNamespace(id=1),
                db=db,
            )
        assert malformed_hardware_error.value.status_code == 422
        assert malformed_hardware_error.value.detail["code"] == "invalid_location"
        worker.enqueue_command.assert_not_called()

        folded = robot_router._fold_store_location_into_item(
            StoreItemCommand(
                location={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
                item={
                    "upca": "123456789014",
                    "name": "Direct item",
                    "dimensions": {"width": 1, "height": 1, "length": 1},
                },
            )
        )
        assert folded.location is None
        assert folded.item is not None
        assert folded.item["location"] == tray_home
        # The folded location reaches the same reservation guard, which reserves only
        # identities; the cell-level refusal is asserted above.
        await robot_router._validate_direct_store_reservation(folded.item, db)


@pytest.mark.anyio
async def test_item_barcodes_and_locations_claim_only_live_trays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _configured_bin("900000000017", "left_module", 0, 0)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config(configured))
    current_user = SimpleNamespace(role=SimpleNamespace(name="admin"), access_level_id=None)

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException, match="Barcode is reserved"):
            await storage_router.create_item(
                storage_router.ItemCreate(upca=configured.barcode, name="Not a bin"),
                db,
                current_user,
            )

        item = await StorageService(db).create_item(
            "123456789017", "Direct item", {"width": 1, "height": 1, "length": 1}
        )
        # A live tray home stays reserved for automatic allocation, while an explicit
        # location edit may use it: the tray's return reports an occupied home instead.
        await storage_router.add_item_location(
            item.upca,
            storage_router.ItemLocationCreate(side="left_module", shelf_idx=0, cell_idx=0),
            db,
            current_user,
        )
        assert await bin_storage.is_reserved_bin_location(("left_module", 0, 0), db) is True

        db.add(
            StorageBin(
                barcode="900000000018",
                side="left_module",
                shelf_idx=0,
                cell_idx=1,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=1,
                status="outside",
                active=False,
            )
        )
        await db.commit()
        # A tray released by config changes stops reserving its home and identity.
        assert await bin_storage.is_reserved_bin_location(("left_module", 0, 1), db) is False
        assert await bin_storage.is_reserved_bin_barcode("900000000018", db) is False
        await storage_router.add_item_location(
            item.upca,
            storage_router.ItemLocationCreate(side="left_module", shelf_idx=0, cell_idx=1),
            db,
            current_user,
        )


@pytest.mark.anyio
async def test_mock_allocation_excludes_live_bin_homes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        db.add_all(
            [
                StorageBin(
                    barcode="900000000011",
                    side="left_module",
                    shelf_idx=0,
                    cell_idx=0,
                    inner_dimensions={"width": 30, "height": 30, "length": 30},
                    max_items=1,
                    status="at_home",
                    active=True,
                ),
                StorageBin(
                    barcode="900000000012",
                    side="left_module",
                    shelf_idx=0,
                    cell_idx=1,
                    inner_dimensions={"width": 30, "height": 30, "length": 30},
                    max_items=1,
                    status="outside",
                    active=True,
                ),
            ]
        )
        await db.commit()

    result = await MockRobotRuntime()._execute_store(
        "mock-reserved-home",
        {
            "command": "store_item",
            "item": {
                "upca": "123456789014",
                "name": "Direct item",
                "dimensions": {"width": 1, "height": 1, "length": 1},
            },
        },
    )
    assert result["location"] == {"side": "left_module", "shelf_idx": 0, "cell_idx": 2}


@pytest.mark.anyio
async def test_mock_allocation_uses_released_bin_homes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        db.add(
            StorageBin(
                barcode="900000000013",
                side="left_module",
                shelf_idx=0,
                cell_idx=0,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=1,
                status="outside",
                active=False,
            )
        )
        await db.commit()

    result = await MockRobotRuntime()._execute_store(
        "mock-released-home",
        {
            "command": "store_item",
            "item": {
                "upca": "123456789014",
                "name": "Direct item",
                "dimensions": {"width": 1, "height": 1, "length": 1},
            },
        },
    )
    assert result["location"] == {"side": "left_module", "shelf_idx": 0, "cell_idx": 0}


@pytest.mark.anyio
async def test_mock_allocation_keeps_empty_bin_config_backward_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    result = await MockRobotRuntime()._execute_store(
        "mock-empty-config",
        {
            "command": "store_item",
            "item": {
                "upca": "123456789015",
                "name": "Direct item",
                "dimensions": {"width": 1, "height": 1, "length": 1},
            },
        },
    )
    assert result["location"] == {"side": "left_module", "shelf_idx": 0, "cell_idx": 0}


@pytest.mark.anyio
async def test_explicit_storage_mode_controls_new_item_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BinStorageService, "sync_config", AsyncMock())
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", None)
    current_user = SimpleNamespace(id=1)

    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        direct_item = await storage.create_item(
            "123456789060", "Direct", {"width": 10, "height": 10, "length": 10}
        )
        bin_item = await storage.create_item(
            "123456789061", "Bin-sized", {"width": 1, "height": 1, "length": 1}
        )
        bin_row = StorageBin(
            barcode="900000000060",
            side="left_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.commit()

        direct = await storage_router.create_bin_workflow(
            storage_router.BinWorkflowCreate(
                operation="store", upca=direct_item.upca, storage_mode="direct"
            ),
            session=db,
            current_user=current_user,
        )
        assert direct == {"storage_mode": "direct", "workflow": None}
        assert bin_row.status == "at_home"
        assert await BinStorageService(db).active_workflows() == []

        selected_bin = await storage_router.create_bin_workflow(
            storage_router.BinWorkflowCreate(
                operation="store", upca=bin_item.upca, storage_mode="bin"
            ),
            session=db,
            current_user=current_user,
        )
        assert selected_bin["storage_mode"] == "bin"
        assert selected_bin["workflow"]["item"]["id"] == bin_item.id
        assert selected_bin["workflow"]["state"] == "failed"
        assert bin_row.status == "outside"


@pytest.mark.anyio
async def test_explicit_bin_mode_rejects_when_no_compatible_bin_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(BinStorageService, "sync_config", AsyncMock())
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", None)

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789062", "Bin-sized", {"width": 1, "height": 1, "length": 1}
        )

        with pytest.raises(HTTPException) as error:
            await storage_router.create_bin_workflow(
                storage_router.BinWorkflowCreate(
                    operation="store", upca=item.upca, storage_mode="bin"
                ),
                session=db,
                current_user=SimpleNamespace(id=1),
            )

        assert error.value.status_code == 409
        assert error.value.detail == "No compatible storage bin is available"
        assert await BinStorageService(db).active_workflows() == []


@pytest.mark.anyio
async def test_allocator_prefers_partially_filled_compatible_bin() -> None:
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        existing = await storage.create_item(
            "111111111111",
            "Existing",
            {"width": 10, "height": 10, "length": 10},
            access_level_id=None,
        )
        target = await storage.create_item(
            "222222222222",
            "Target",
            {"width": 20, "height": 10, "length": 15},
            access_level_id=None,
        )
        first = StorageBin(
            barcode="900000000001",
            side="left_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions={"width": 30, "height": 20, "length": 30},
            max_items=3,
            status="at_home",
            active=True,
        )
        second = StorageBin(
            barcode="900000000002",
            side="left_module",
            shelf_idx=0,
            cell_idx=1,
            inner_dimensions={"width": 25, "height": 20, "length": 25},
            max_items=3,
            status="at_home",
            active=True,
        )
        db.add_all([first, second])
        await db.flush()
        db.add(StorageBinItem(bin_id=first.id, item_id=existing.id))
        await db.commit()

        allocated = await BinStorageService(db).allocate_bin(target)
        assert allocated is not None
        assert allocated.id == first.id


@pytest.mark.anyio
async def test_allocator_enforces_configured_item_footprint_and_bin_height() -> None:
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        too_wide = await storage.create_item(
            "222222222223",
            "Too wide",
            {"width": 21, "height": 10, "length": 20},
        )
        too_tall = await storage.create_item(
            "222222222224",
            "Too tall",
            {"width": 20, "height": 21, "length": 20},
        )
        bin_row = StorageBin(
            barcode="900000000063",
            side="left_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions={"width": 100, "height": 20, "length": 100},
            max_item_dimensions={"width": 20, "length": 20},
            max_items=10,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.commit()

        service = BinStorageService(db)
        assert await service.allocate_bin(too_wide) is None
        assert await service.allocate_bin(too_tall) is None


@pytest.mark.anyio
async def test_allocator_rejects_bin_without_remaining_volume() -> None:
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        existing = await storage.create_item(
            "222222222225",
            "Existing",
            {"width": 10, "height": 10, "length": 9},
        )
        target = await storage.create_item(
            "222222222226",
            "Target",
            {"width": 10, "height": 2, "length": 10},
        )
        full_bin = StorageBin(
            barcode="900000000064",
            side="left_module",
            shelf_idx=0,
            cell_idx=0,
            inner_dimensions={"width": 10, "height": 10, "length": 10},
            max_item_dimensions={"width": 10, "length": 10},
            max_items=10,
            status="at_home",
            active=True,
        )
        db.add(full_bin)
        await db.flush()
        db.add(StorageBinItem(bin_id=full_bin.id, item_id=existing.id))
        await db.commit()

        service = BinStorageService(db)
        assert await service.allocate_bin(target) is None
        summary = await service.serialize_bin(full_bin)
        assert summary["max_item_dimensions"] == {"width": 10.0, "length": 10.0}
        assert summary["used_volume"] == 900
        assert summary["available_volume"] == 100


@pytest.mark.anyio
async def test_stale_or_direct_robot_results_do_not_change_active_workflow() -> None:
    worker = FakeWorker()
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789018", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000018",
            side="right_module",
            shelf_idx=1,
            cell_idx=2,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.commit()
        service = BinStorageService(db)
        workflow = await service.begin("store", item, None)
        assert workflow is not None

        assert not await service.handle_robot_result(
            {
                "command": "retrieve_item",
                "message": "success",
                "location": {"side": "right_module", "shelf_idx": 1, "cell_idx": 2},
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "retrieving_bin"

        assert not await service.handle_robot_result(
            {
                "command": "retrieve_item",
                "message": "error",
                "request_id": "stale-workflow",
                "upca": bin_row.barcode,
                "error": "motor fault",
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "retrieving_bin"


@pytest.mark.anyio
async def test_parked_tray_interaction_allows_item_and_location_mutations() -> None:
    current_user = SimpleNamespace(role=SimpleNamespace(name="admin"), access_level_id=None)
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789017", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000017",
                side="right_module",
                shelf_idx=1,
                cell_idx=2,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=2,
                status="at_home",
                active=True,
            )
        )
        await db.commit()
        service = BinStorageService(db)
        workflow = await service.begin("store", item, None)
        assert workflow is not None
        await service.transition(workflow, "awaiting_operator")

        # A parked tray holds the item for its own completion, not against every edit.
        await storage_router.create_item(
            storage_router.ItemCreate(
                upca=item.upca,
                name="Changed",
                dimensions={"width": 10, "height": 10, "length": 10},
            ),
            db,
            current_user,
        )
        await storage_router.update_item(
            item.upca, storage_router.ItemUpdate(name="Changed again"), db, current_user
        )
        await storage_router.add_item_location(
            item.upca,
            storage_router.ItemLocationCreate(side="left_module", shelf_idx=0, cell_idx=0),
            db,
            current_user,
        )

        # Completing the interaction is what rejects the conflicting direct location.
        with pytest.raises(BinWorkflowConflict, match="direct storage location"):
            await service.close(workflow)

        await storage_router.remove_item_location(item.upca, "left_module", 0, 0, db, current_user)
        await service.close(workflow)
        assert workflow.state == "completed"

        # Deletion stays out of reach once the item is a tray member with retained history.
        with pytest.raises(HTTPException, match="Retrieve the item before deleting it"):
            await storage_router.delete_item(item.upca, db)


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["completed", "cancelled"])
async def test_delete_item_with_historical_bin_workflow_returns_clear_conflict(state: str) -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789019", "Historical target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000019",
            side="right_module",
            shelf_idx=1,
            cell_idx=3,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.flush()
        workflow = StorageBinWorkflow(
            id=f"historical-{state}",
            operation="retrieve",
            state=state,
            bin_id=bin_row.id,
            item_id=item.id,
            history=[{"state": state}],
        )
        db.add(workflow)
        await db.commit()

        with pytest.raises(HTTPException) as error:
            await storage_router.delete_item(item.upca, db)

        assert error.value.status_code == 409
        assert (
            error.value.detail
            == "Item cannot be deleted because storage bin workflow history is retained"
        )
        assert await db.get(StorageBinWorkflow, workflow.id) is not None
        assert await StorageService(db).get_item(item.upca) is not None


@pytest.mark.anyio
async def test_return_rejects_occupied_tray_home(monkeypatch) -> None:
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        tray = trays[0]
        barcode = tray.barcode
        home = (tray.side, tray.shelf_idx, tray.cell_idx)
        service = BinStorageService(db)
        checkout = await service.begin("retrieve", items[0], None)
        assert checkout is not None
        await service.transition(checkout, "awaiting_operator")
        await service.close(checkout)
        returned = await service.begin_return(barcode)
        assert returned is not None
        returned_id = returned.id

        occupant = await StorageService(db).create_item(
            "123456788099", "Home occupant", {"width": 10, "length": 10, "height": 10}
        )
        occupant_upca = occupant.upca
        # An item recorded in the cell the tray returns to is rejected at confirmation.
        await StorageService(db).add_item_location(occupant_upca, *home)
        with pytest.raises(BinWorkflowConflict, match="home cell is occupied"):
            await service.prepare_return(returned, [])

        await StorageService(db).remove_item_location(occupant_upca, *home)
        refreshed = await db.get(StorageBinWorkflow, returned_id)
        assert refreshed is not None
        assert await service.prepare_return(refreshed, []) is True


@pytest.mark.anyio
async def test_member_upsert_preserves_omitted_bin_invariant_fields() -> None:
    current_user = SimpleNamespace(role=SimpleNamespace(name="admin"), access_level_id=None)
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        access_level = AccessLevel(name="Restricted")
        db.add(access_level)
        await db.flush()
        item = await storage.create_item(
            "123456789018",
            "Original",
            {"width": 10, "height": 11, "length": 12},
            weight=13,
            metadata={"category": "preserve"},
            access_level_id=access_level.id,
        )
        bin_row = StorageBin(
            barcode="900000000018",
            side="right_module",
            shelf_idx=1,
            cell_idx=2,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.flush()
        db.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await db.commit()

        updated = await storage_router.create_item(
            storage_router.ItemCreate(upca=item.upca, name="Renamed"), db, current_user
        )

        assert updated.name == "Renamed"
        assert updated.dimensions == {"width": 10, "height": 11, "length": 12}
        assert updated.weight == 13
        assert updated.metadata == {"category": "preserve"}
        assert updated.access_level_id == access_level.id


async def setup_trays(db):
    storage = StorageService(db)
    items = [
        await storage.create_item(
            f"12345678800{i}", f"Item {i}", {"width": 10, "length": 10, "height": 10}
        )
        for i in range(3)
    ]
    trays = [
        StorageBin(
            barcode=f"90000000800{i}",
            side="left_module",
            shelf_idx=0,
            cell_idx=i,
            inner_dimensions={"width": 100, "length": 150, "height": 100},
            compartment_layout={"columns": 2, "rows": 3},
            max_items=6,
            status="at_home",
            active=True,
        )
        for i in range(2)
    ]
    db.add_all(trays)
    await db.flush()
    db.add_all(
        [
            StorageBinItem(bin_id=trays[0].id, item_id=items[0].id, compartment=1),
            StorageBinItem(bin_id=trays[0].id, item_id=items[1].id, compartment=2),
            StorageBinItem(bin_id=trays[1].id, item_id=items[2].id, compartment=1),
        ]
    )
    await db.commit()
    return items, trays


@pytest.mark.anyio
async def test_close_takes_item_and_leaves_other_contents_visible_outside():
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.transition(w, "awaiting_operator")
        await service.close(w)
        assert w.state == "completed"
        assert trays[0].status == "outside"
        assert await service.item_bin(items[0].id) is None
        user = SimpleNamespace(role=SimpleNamespace(name="admin"), access_level_id=None)
        payload = await storage_router._build_item_payload_with_bin(items[1], [], user, db)
        assert payload.bin["status"] == "outside"
        assert payload.can_retrieve is False
        with pytest.raises(BinWorkflowConflict):
            await service.begin("retrieve", items[1], None)
        await db.refresh(items[2])
        assert await service.begin("retrieve", items[2], None) is not None
        async with manual_motion_guard("gripper", worker=FakeWorker()):
            pass


@pytest.mark.anyio
@pytest.mark.parametrize("select_item", [True, False], ids=["subset", "empty"])
async def test_barcode_return_defaults_all_known_items_and_applies_selection(select_item):
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.transition(w, "awaiting_operator")
        await service.close(w)
        returned = await service.begin_return(trays[0].barcode)
        payload = await service.serialize_workflow(returned)
        assert {i["id"] for i in payload["contents"]} == {items[0].id, items[1].id}
        assert all(i["confirmed"] for i in payload["contents"])
        assert returned.item_id is None
        assert (await service.begin_return(trays[0].barcode)).id == returned.id
        selected_ids = [items[0].id] if select_item else []
        expected_selection = {items[0].id: select_item, items[1].id: False}
        assert await service.prepare_return(returned, selected_ids)
        assert not await service.prepare_return(returned, selected_ids)
        await db.refresh(returned)
        payload = await service.serialize_workflow(returned)
        assert payload["state"] == "returning_bin"
        assert {i["id"]: i["confirmed"] for i in payload["contents"]} == expected_selection
        worker = FakeWorker()
        await service.publish_workflow(worker, returned)
        assert worker.events[-1] == {"kind": "bin_workflow_updated", "payload": payload}
        await service.complete_return(returned, 425.5)
        await service.complete_return(returned)
        assert trays[0].status == "at_home"
        assert trays[0].gross_weight == 425.5
        await db.refresh(returned)
        payload = await service.serialize_workflow(returned)
        assert payload["state"] == "completed"
        assert {i["id"]: i["confirmed"] for i in payload["contents"]} == expected_selection
        await service.publish_workflow(worker, returned)
        assert worker.events[-1] == {"kind": "bin_workflow_updated", "payload": payload}
        assert await service.item_bin(items[1].id) is None
        if select_item:
            assert (await service.item_bin(items[0].id)).id == trays[0].id
        else:
            assert await service.item_bin(items[0].id) is None
        with pytest.raises(BinWorkflowConflict, match="already in"):
            await service.begin_return(trays[0].barcode)


@pytest.mark.anyio
async def test_mock_checkout_close_and_barcode_return_simulates_placement(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MockRobotRuntime()
    active_commands = AsyncMock(return_value=False)
    monkeypatch.setattr(runtime, "has_active_commands", active_commands)
    monkeypatch.setattr(storage_router, "get_mock_robot_runtime", lambda: runtime)
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.mock)
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", None)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        upca = items[0].upca
        selected_id, omitted_id = items[0].id, items[1].id
        barcode, tray_id = trays[0].barcode, trays[0].id

    statuses, _ = await runtime.subscribe_status()
    try:
        checkout = await client.post(
            "/api/storage/bin-workflows", json={"operation": "retrieve", "upca": upca},
        )
        assert checkout.status_code == 200, checkout.text
        checkout_id = checkout.json()["workflow"]["id"]
        assert json.loads(statuses.get_nowait())["robot_status"]["scale"]["occupied"] is True

        closed = await client.post(f"/api/storage/bin-workflows/{checkout_id}/close")
        assert closed.status_code == 200, closed.text
        assert closed.json()["workflow"]["state"] == "completed"
        assert json.loads(statuses.get_nowait())["robot_status"]["scale"]["occupied"] is False

        returned = await client.post(
            "/api/storage/bin-workflows/returns", json={"barcode": barcode},
        )
        assert returned.status_code == 200, returned.text
        workflow = returned.json()["workflow"]
        assert {item["id"] for item in workflow["contents"]} == {selected_id, omitted_id}
        assert all(item["confirmed"] for item in workflow["contents"])
        assert json.loads(statuses.get_nowait())["robot_status"]["scale"]["occupied"] is True

        store_url = f"/api/storage/bin-workflows/{workflow['id']}/store"
        selection = {"confirmed_item_ids": [selected_id]}
        active_commands.return_value = True
        blocked = await client.post(store_url, json=selection)
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "Robot has queued commands"
        active_commands.return_value = False
        stored = await client.post(store_url, json=selection)
        assert stored.status_code == 200, stored.text
        assert stored.json()["workflow"]["state"] == "completed"
        assert {
            item["id"]: item["confirmed"] for item in stored.json()["workflow"]["contents"]
        } == {selected_id: True, omitted_id: False}
        assert json.loads(statuses.get_nowait())["robot_status"]["scale"]["occupied"] is False
    finally:
        await runtime.unsubscribe_status(statuses)

    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        tray = await db.get(StorageBin, tray_id)
        assert tray.status == "at_home"
        assert (await service.item_bin(selected_id)).id == tray_id
        assert await service.item_bin(omitted_id) is None


@pytest.mark.anyio
async def test_store_close_assigns_compartment_without_return_motion():
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        target = await StorageService(db).create_item(
            "123456788099", "New", {"width": 10, "length": 10, "height": 10}
        )
        service = BinStorageService(db)
        w = await service.begin("store", target, None)
        assert w.compartment == 3
        await service.transition(w, "awaiting_operator")
        await service.close(w)
        await service.close(w)
        assert (await service.item_bin(target.id)).status == "outside"
        assert (
            await db.scalar(
                select(StorageBinItem.compartment).where(StorageBinItem.item_id == target.id)
            )
            == 3
        )
        assert await service.active_workflows() == []


@pytest.mark.anyio
async def test_return_requires_occupancy_then_enqueues_once(monkeypatch):
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.development)
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    monkeypatch.setattr(bin_storage, "enqueue_internal", AsyncMock())
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.transition(w, "awaiting_operator")
        await service.close(w)
        returned = await service.begin_return(trays[0].barcode)
        request = storage_router.BinWorkflowReturn(confirmed_item_ids=[])
        worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=False)
        with pytest.raises(HTTPException, match="Place the tray"):
            await storage_router.store_returned_tray(returned.id, request, db)
        worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=True)
        await storage_router.store_returned_tray(returned.id, request, db)
        await storage_router.store_returned_tray(returned.id, request, db)
        assert bin_storage.enqueue_internal.await_count == 1
        assert returned.state == "returning_bin"


@pytest.mark.anyio
@pytest.mark.parametrize("close_first", [False, True], ids=["store-wins", "close-wins"])
async def test_return_close_and_store_use_persisted_state_across_sessions(monkeypatch, close_first):
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.development)
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    enqueue = AsyncMock()
    monkeypatch.setattr(bin_storage, "enqueue_internal", enqueue)

    async with AsyncSessionLocal() as close_db:
        items, trays = await setup_trays(close_db)
        service = BinStorageService(close_db)
        checkout = await service.begin("retrieve", items[0], None)
        await service.transition(checkout, "awaiting_operator")
        await service.close(checkout)
        returned = await service.begin_return(trays[0].barcode)
        workflow_id, tray_id = returned.id, trays[0].id
        selected_id, omitted_id = items[0].id, items[1].id
        request = storage_router.BinWorkflowReturn(confirmed_item_ids=[selected_id])

        async with AsyncSessionLocal() as store_db:
            # Both requests have loaded awaiting_operator before either wins.
            stale_store = await store_db.get(StorageBinWorkflow, workflow_id)
            assert stale_store.state == returned.state == "awaiting_operator"
            if close_first:
                await service.close(returned)
                result = await storage_router.store_returned_tray(workflow_id, request, store_db)
                assert result["workflow"]["state"] == "cancelled"
                enqueue.assert_not_awaited()
            else:
                await storage_router.store_returned_tray(workflow_id, request, store_db)
                assert returned.state == "awaiting_operator"
                with pytest.raises(BinWorkflowConflict, match="Wait for tray motion"):
                    await service.close(returned)
                assert returned.state == "returning_bin"
                assert (await service.begin_return("900000008000")).id == workflow_id
                await storage_router.store_returned_tray(workflow_id, request, store_db)
                enqueue.assert_awaited_once()

    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        returned = await db.get(StorageBinWorkflow, workflow_id)
        if close_first:
            await service.close(returned)
            assert returned.state == "cancelled"
            assert await service.item_bin(selected_id) is None
            assert (await service.item_bin(omitted_id)).id == tray_id
            assert (await db.get(StorageBin, tray_id)).status == "outside"
        else:
            assert await service.handle_robot_result(
                {"request_id": workflow_id, "command": "store_item", "message": "success"},
                worker,
            )
            assert returned.state == "completed"
            assert returned.resolution["physical_return_completed"] is True
            assert (await db.get(StorageBin, tray_id)).status == "at_home"
            assert (await service.item_bin(selected_id)).id == tray_id
            assert await service.item_bin(omitted_id) is None
            with pytest.raises(BinWorkflowConflict, match="already in"):
                await service.begin_return("900000008000")


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["retrieve", "store"])
async def test_stale_checkout_close_does_not_reapply_membership_after_return(operation):
    async with AsyncSessionLocal() as stale_db:
        items, trays = await setup_trays(stale_db)
        target = items[0]
        if operation == "store":
            target = await StorageService(stale_db).create_item(
                "123456788099", "New", {"width": 10, "length": 10, "height": 10}
            )
        service = BinStorageService(stale_db)
        checkout = await service.begin(operation, target, None)
        await service.transition(checkout, "awaiting_operator")
        target_id, tray_id = target.id, trays[0].id

        async with AsyncSessionLocal() as winner_db:
            winner = BinStorageService(winner_db)
            current = await winner_db.get(StorageBinWorkflow, checkout.id)
            await winner.close(current)
            returned = await winner.begin_return("900000008000")
            # Reverse the checkout's membership change during a later return.
            assert await winner.prepare_return(
                returned, [target_id] if operation == "retrieve" else []
            )
            await winner.complete_return(returned)
            completed_history = current.history

        assert checkout.state == "awaiting_operator"
        await service.close(checkout)
        await service.close(checkout)
        assert checkout.state == "completed"
        assert checkout.history == completed_history
        member = await service.item_bin(target_id)
        if operation == "retrieve":
            assert member.id == tray_id
        else:
            assert member is None
        await stale_db.refresh(trays[0])
        assert trays[0].status == "at_home"
        assert await service.active_workflows() == []


@pytest.mark.anyio
async def test_return_rejects_unexpected_and_cross_tray_items_before_motion():
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.transition(w, "awaiting_operator")
        await service.close(w)
        returned = await service.begin_return(trays[0].barcode)
        rid, other_id = returned.id, items[2].id
        with pytest.raises(BinWorkflowConflict, match="unexpected|compartment"):
            await service.prepare_return(returned, [other_id])
        await db.refresh(returned)
        assert returned.state == "awaiting_operator"
        await service.close(returned)
        assert returned.state == "cancelled"
        assert (await service.begin_return("900000008000")).id != rid


@pytest.mark.anyio
async def test_interrupted_motion_fails_without_global_lock_or_automatic_replay():
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.recover_interrupted_workflows()
        assert w.state == "failed"
        assert trays[0].status == "outside"
        assert await service.has_unfinished_motion()
        assert await service.begin("retrieve", items[2], None)
        async with manual_motion_guard("gripper", worker=FakeWorker()):
            pass


@pytest.mark.anyio
async def test_restart_finalizes_recorded_physical_return_without_motion():
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.transition(w, "awaiting_operator")
        await service.close(w)
        returned = await service.begin_return(trays[0].barcode)
        await service.prepare_return(returned, [])
        returned.resolution = {**returned.resolution, "physical_return_completed": True}
        await db.commit()
        await service.recover_interrupted_workflows()
        assert returned.state == "completed"
        assert trays[0].status == "at_home"
        assert await service.item_bin(items[1].id) is None


@pytest.mark.anyio
async def test_same_item_cannot_checkout_two_trays():
    async with AsyncSessionLocal() as db:
        await setup_trays(db)
        target = await StorageService(db).create_item(
            "123456788098", "New", {"width": 10, "length": 10, "height": 10}
        )
        service = BinStorageService(db)
        assert await service.begin("store", target, None)
        with pytest.raises(BinWorkflowConflict, match="already in progress"):
            await service.begin("store", target, None)
        assert len(await service.active_workflows()) == 1
        assert (
            len((await db.scalars(select(StorageBin).where(StorageBin.status == "outside"))).all())
            == 1
        )


@pytest.mark.anyio
async def test_direct_store_reserves_only_tray_members(monkeypatch):
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        await setup_trays(db)
        target = await StorageService(db).create_item(
            "123456788077", "Pending tray item", {"width": 10, "length": 10, "height": 10}
        )
        service = BinStorageService(db)
        w = await service.begin("store", target, None)
        assert w is not None
        await service.transition(w, "awaiting_operator")
        # A parked interaction does not reserve its item, and unrelated items stay eligible.
        await robot_router._validate_direct_store_reservation({"upca": target.upca}, db)
        await robot_router._validate_direct_store_reservation({"upca": "123456788078"}, db)
        await service.close(w)
        with pytest.raises(HTTPException) as stored:
            await robot_router._validate_direct_store_reservation({"upca": target.upca}, db)
        assert stored.value.status_code == 409
        assert stored.value.detail["code"] == "reserved_tray_item"


@pytest.mark.anyio
async def test_direct_store_allows_return_candidate_until_confirmation(monkeypatch):
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        upca = items[0].upca
        item_id = items[0].id
        service = BinStorageService(db)
        checkout = await service.begin("retrieve", items[0], None)
        await service.transition(checkout, "awaiting_operator")
        await service.close(checkout)
        assert await service.item_bin(item_id) is None
        returned = await service.begin_return(trays[0].barcode)
        returned_id = returned.id
        # The open return dialog names the candidate but does not reserve it for storage.
        await robot_router._validate_direct_store_reservation({"upca": upca}, db)
        await StorageService(db).add_item_location(upca, "left_module", shelf_idx=5, cell_idx=0)
        # Confirming the return, not the open dialog, rejects the conflicting location.
        with pytest.raises(BinWorkflowConflict, match="direct storage location"):
            await service.prepare_return(returned, [item_id])
        await StorageService(db).remove_item_location(upca, "left_module", 5, 0)
        confirmed = await db.get(StorageBinWorkflow, returned_id)
        assert confirmed is not None
        assert await service.prepare_return(confirmed, [item_id]) is True


@pytest.mark.anyio
async def test_mock_direct_admission_excludes_overlapping_tray_start(monkeypatch):
    monkeypatch.setattr(storage_router, "can_user_retrieve_item", lambda *args: True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def validate(*args):
        entered.set()
        await release.wait()

    runtime = SimpleNamespace(
        replay=AsyncMock(return_value=None),
        enqueue=AsyncMock(return_value=(SimpleNamespace(command_id="direct", status="queued"), False)),
        has_active_commands=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.mock)
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.mock)
    monkeypatch.setattr(robot_router, "get_mock_robot_runtime", lambda: runtime)
    monkeypatch.setattr(storage_router, "get_mock_robot_runtime", lambda: runtime)
    monkeypatch.setattr(robot_router, "_validate_mock_store", validate)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config())
    async with AsyncSessionLocal() as db:
        items, _ = await setup_trays(db)
        upca = items[0].upca
    async with AsyncSessionLocal() as direct_db, AsyncSessionLocal() as tray_db:
        command = asyncio.create_task(robot_router.robot_commands(
            StoreItemCommand(item={"upca": "123456788078"}),
            worker=None, current_user=SimpleNamespace(id=1), db=direct_db,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            with pytest.raises(HTTPException) as rejected:
                await storage_router.create_bin_workflow(
                    storage_router.BinWorkflowCreate(operation="retrieve", upca=upca),
                    session=tray_db, current_user=SimpleNamespace(id=1),
                )
            assert rejected.value.status_code == 409
        finally:
            release.set()
            await command
