from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from openshelf_os.exceptions import (
    GRIP_DROPPED_CODE,
    GRIP_IO_BOX_FAILED_CODE,
    GRIP_PICK_FAILED_CODE,
)
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.robot.commands import StoreItemCommand
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core.bin_storage import BinStorageService, BinWorkflowConflict
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import StorageBin, StorageBinItem, StorageBinWorkflow
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


def _bin_config(*entries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(storage=SimpleNamespace(bins=list(entries)))


def _configured_bin(barcode: str, side: str, shelf_idx: int, cell_idx: int) -> SimpleNamespace:
    return SimpleNamespace(
        barcode=barcode,
        home_location=SimpleNamespace(side=side, shelf_idx=shelf_idx, cell_idx=cell_idx),
    )


@pytest.mark.anyio
async def test_direct_store_rejects_live_bin_barcode_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _configured_bin("900000000010", "front_module", 0, 0)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config(configured))
    tray_home = {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as barcode_error:
            await robot_router._validate_direct_store_reservation({"barcode": configured.barcode}, db)
        assert barcode_error.value.status_code == 409
        assert barcode_error.value.detail["code"] == "reserved_bin_barcode"

        # A tray home is a usable target for the reservation guard in both modes: the tray's
        # return reports the occupied cell instead of the store being refused up front. The
        # cell-level check below still refuses it, as production's maintenance status does.
        with pytest.raises(HTTPException) as home_error:
            await robot_router._validate_mock_store(
                StoreItemCommand(
                    item={
                        "barcode": "123456789012",
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
            {"barcode": "123456789013", "location": tray_home}, db
        )

        with pytest.raises(HTTPException) as malformed_mock_error:
            await robot_router._validate_mock_store(
                StoreItemCommand(
                    item={
                        "barcode": "123456789015",
                        "name": "Direct item",
                        "dimensions": {"width": 1, "height": 1, "length": 1},
                        "location": {"side": "front_module", "shelf_idx": False, "cell_idx": 0},
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
                    location={"side": "front_module", "shelf_idx": "0", "cell_idx": 0},
                    item={
                        "barcode": "123456789016",
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
                location={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
                item={
                    "barcode": "123456789014",
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
async def test_stale_or_direct_robot_results_do_not_change_active_workflow() -> None:
    worker = FakeWorker()
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789018", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000018",
            side="back_module",
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
                "location": {"side": "back_module", "shelf_idx": 1, "cell_idx": 2},
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "retrieving_bin"

        assert not await service.handle_robot_result(
            {
                "command": "retrieve_item",
                "message": "error",
                "request_id": "stale-workflow",
                "barcode": bin_row.barcode,
                "error": "motor fault",
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "retrieving_bin"


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
        occupant_barcode = occupant.barcode
        # An item recorded in the cell the tray returns to is rejected at confirmation.
        await StorageService(db).add_item_location(occupant_barcode, *home)
        with pytest.raises(BinWorkflowConflict, match="home cell is occupied"):
            await service.prepare_return(returned, [])

        await StorageService(db).remove_item_location(occupant_barcode, *home)
        refreshed = await db.get(StorageBinWorkflow, returned_id)
        assert refreshed is not None
        assert await service.prepare_return(refreshed, []) is True


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
            side="front_module",
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


def _motion_failure(workflow, command, code):
    return {
        "request_id": workflow.id,
        "command": command,
        "message": "failed",
        "error": "Motor stalled",
        "code": code,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code,is_recovered",
    [
        ("execution_error", True),
        ("command_interrupted", True),
        ("execution_failed", False),
        (GRIP_DROPPED_CODE, True),
        (GRIP_IO_BOX_FAILED_CODE, True),
    ],
)
async def test_crash_reset_returns_only_crash_interrupted_trays_home(code, is_recovered):
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.handle_robot_result(_motion_failure(w, "retrieve_item", code), FakeWorker())
        assert w.state == "failed"
        assert trays[0].status == "outside"
        interrupted = [workflow.id for workflow in await service.crash_interrupted_workflows()]
        assert interrupted == ([w.id] if is_recovered else [])

        results = await service.recover_crash_interrupted()

        assert [(workflow.id, error) for workflow, error in results] == (
            [(w.id, None)] if is_recovered else []
        )
        await db.refresh(trays[0])
        assert trays[0].status == ("at_home" if is_recovered else "outside")
        assert await service.has_unfinished_motion() is (is_recovered is False)
        # The delivery never reached the operator, so the item is still in the tray.
        assert (await service.item_bin(items[0].id)).id == trays[0].id


@pytest.mark.anyio
async def test_a_pick_that_never_lifted_the_tray_leaves_no_unfinished_motion():
    """A failed grip check at the shelf cancels the motion with the tray still at home."""
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        w = await service.begin("retrieve", items[0], None)
        await service.handle_robot_result(
            _motion_failure(w, "retrieve_item", GRIP_PICK_FAILED_CODE), FakeWorker()
        )

        assert w.state == "cancelled"
        await db.refresh(trays[0])
        assert trays[0].status == "at_home"
        assert await service.crash_interrupted_workflows() == []
        assert await service.has_unfinished_motion() is False
        assert (await service.item_bin(items[0].id)).id == trays[0].id


@pytest.mark.anyio
async def test_crash_reset_records_contents_confirmed_for_an_interrupted_return():
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        taken = await service.begin("retrieve", items[0], None)
        await service.transition(taken, "awaiting_operator")
        await service.close(taken)
        returned = await service.begin_return(trays[0].barcode)
        # The operator put the item back and removed the other one before the return crashed.
        assert await service.prepare_return(returned, [items[0].id])
        await service.handle_robot_result(
            _motion_failure(returned, "store_item", "execution_error"), FakeWorker()
        )
        assert returned.state == "failed"

        [(workflow, error)] = await service.recover_crash_interrupted()

        assert (workflow.id, error) == (returned.id, None)
        await db.refresh(trays[0])
        assert trays[0].status == "at_home"
        members = await db.execute(
            select(StorageBinItem.item_id, StorageBinItem.compartment).where(
                StorageBinItem.bin_id == trays[0].id
            )
        )
        assert members.all() == [(items[0].id, 1)]


@pytest.mark.anyio
async def test_crash_recovery_endpoint_leaves_conflicting_return_outside(monkeypatch):
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", None)
    async with AsyncSessionLocal() as db:
        items, trays = await setup_trays(db)
        service = BinStorageService(db)
        returned = await service.begin_return((await _checked_out_tray(service, items, trays)).barcode)
        assert await service.prepare_return(returned, [items[1].id])
        await service.handle_robot_result(
            _motion_failure(returned, "store_item", "command_interrupted"), FakeWorker()
        )
        listed = await storage_router.get_crash_interrupted_bin_workflows(db)
        assert [entry["bin"]["barcode"] for entry in listed["workflows"]] == [trays[0].barcode]
        occupant = await StorageService(db).create_item(
            "123456788098", "Home occupant", {"width": 10, "length": 10, "height": 10}
        )
        await StorageService(db).add_item_location(
            occupant.barcode, trays[0].side, trays[0].shelf_idx, trays[0].cell_idx
        )

        response = await storage_router.recover_crash_interrupted_bin_workflows(db)

        assert response["recovered"] == []
        assert [entry["error"] for entry in response["unresolved"]] == [
            "Tray home cell is occupied by an item"
        ]
        await db.refresh(trays[0])
        assert trays[0].status == "outside"


async def _checked_out_tray(service, items, trays):
    checkout = await service.begin("retrieve", items[0], None)
    await service.transition(checkout, "awaiting_operator")
    await service.close(checkout)
    return trays[0]


@pytest.mark.anyio
async def test_failed_tray_store_keeps_tray_intent_and_item_stays_deletable():
    """A failed tray store must not make its item undeletable; the workflow record stays."""
    async with AsyncSessionLocal() as db:
        await setup_trays(db)
        item = await StorageService(db).create_item(
            "123456788009", "Loose item", {"width": 10, "length": 10, "height": 10}
        )
        service = BinStorageService(db)
        w = await service.begin("store", item, None)
        request = storage_router.BarcodeRequest(barcode=item.barcode)

        with pytest.raises(HTTPException) as active:
            await storage_router.delete_item(request, session=db)
        assert active.value.status_code == 409

        await service.handle_robot_result(
            _motion_failure(w, "retrieve_item", GRIP_PICK_FAILED_CODE), FakeWorker()
        )
        # The next store of this item goes back to a tray instead of a whole cell.
        assert item.item_metadata["storage_mode"] == "bin"
        await storage_router.delete_item(request, session=db)

        assert await StorageService(db).get_item("123456788009") is None
        await db.refresh(w)
        assert w.item_id is None
        assert w.resolution["deleted_item"]["barcode"] == "123456788009"
