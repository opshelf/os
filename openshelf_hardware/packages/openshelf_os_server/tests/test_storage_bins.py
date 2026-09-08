import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.robot.commands import HomeCommand, StoreItemCommand
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
async def test_recover_interrupted_workflows_preserves_scan_wait_and_terminal_states() -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789000", "Recovery target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000000",
            side="right_module",
            shelf_idx=1,
            cell_idx=0,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.flush()
        workflows = {
            state: StorageBinWorkflow(
                id=f"startup-recovery-{state}",
                operation="retrieve",
                state=state,
                bin_id=bin_row.id,
                item_id=item.id,
                history=[{"state": state}],
            )
            for state in (
                "retrieving_bin",
                "returning_bin",
                "retry_claimed",
                "reconcile_claimed",
                "awaiting_item_scan",
                "awaiting_bin_scan",
                "awaiting_operator",
                "completed",
            )
        }
        for state in ("retrieving_bin", "returning_bin", "retry_claimed", "reconcile_claimed"):
            workflows[state].recovery_claim = f"stale-{state}-claim"
        db.add_all(workflows.values())
        await db.commit()

        service = BinStorageService(db)
        await service.recover_interrupted_workflows()

        recovered_states = ("retrieving_bin", "returning_bin", "retry_claimed", "reconcile_claimed", "awaiting_item_scan", "awaiting_bin_scan")
        restart_error = "Robot process restarted during motion; physical state must be reconciled"
        for state in recovered_states:
            workflow = workflows[state]
            assert workflow.state == "recovery_required"
            assert workflow.recovery_claim is None
            assert workflow.error == restart_error
            assert [entry["state"] for entry in workflow.history] == [state, "recovery_required"]
            assert workflow.history[-1]["detail"] == restart_error
        assert workflows["awaiting_operator"].state == "awaiting_operator"
        assert workflows["completed"].state == "completed"

        await service.recover_interrupted_workflows()
        for state in recovered_states:
            assert [entry["state"] for entry in workflows[state].history] == [
                state,
                "recovery_required",
            ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "workflow_state",
    ("retry_claimed", "reconcile_claimed", "retrieving_bin", "returning_bin"),
)
async def test_startup_recovery_retains_global_lock_after_interrupted_claim(
    workflow_state: str,
) -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789001", "Recovery lock target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000001",
            side="right_module",
            shelf_idx=1,
            cell_idx=0,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.flush()
        workflow = StorageBinWorkflow(
            id=f"startup-recovery-lock-{workflow_state}",
            operation="retrieve",
            state=workflow_state,
            lock_key="global",
            recovery_claim=f"stale-{workflow_state}-claim",
            bin_id=bin_row.id,
            item_id=item.id,
            history=[{"state": workflow_state}],
        )
        db.add(workflow)
        await db.commit()

        await BinStorageService(db).recover_interrupted_workflows()

        assert workflow.state == "recovery_required"
        assert workflow.recovery_claim is None
        assert workflow.lock_key == "global"


@pytest.mark.anyio
async def test_direct_store_rejects_reserved_bin_barcode_and_home_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = _configured_bin("900000000010", "left_module", 0, 0)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config(configured))

    async with AsyncSessionLocal() as db:
        with pytest.raises(HTTPException) as barcode_error:
            await robot_router._validate_direct_store_reservation({"upca": configured.barcode}, db)
        assert barcode_error.value.status_code == 409
        assert barcode_error.value.detail["code"] == "reserved_bin_barcode"

        with pytest.raises(HTTPException) as home_error:
            await robot_router._validate_mock_store(
                StoreItemCommand(
                    item={
                        "upca": "123456789012",
                        "name": "Direct item",
                        "dimensions": {"width": 1, "height": 1, "length": 1},
                        "location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
                    }
                ),
                db,
            )
        assert home_error.value.status_code == 409
        assert home_error.value.detail["code"] == "reserved_bin_home"

        worker = MagicMock()
        worker.needs_home.return_value = False
        monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
        monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
        with pytest.raises(HTTPException) as hardware_error:
            await robot_router.robot_commands(
                StoreItemCommand(
                    item={
                        "upca": "123456789013",
                        "name": "Direct item",
                        "dimensions": {"width": 1, "height": 1, "length": 1},
                        "location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
                    }
                ),
                worker=worker,
                current_user=SimpleNamespace(id=1),
                db=db,
            )
        assert hardware_error.value.status_code == 409
        assert hardware_error.value.detail["code"] == "reserved_bin_home"
        worker.enqueue_command.assert_not_called()


@pytest.mark.anyio
async def test_item_crud_reserves_configured_and_quarantined_bin_homes_before_sync(
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
        with pytest.raises(HTTPException, match="reserved as a storage bin home"):
            await storage_router.add_item_location(
                item.upca,
                storage_router.ItemLocationCreate(side="left_module", shelf_idx=0, cell_idx=0),
                db,
                current_user,
            )

        db.add(
            StorageBin(
                barcode="900000000018",
                side="left_module",
                shelf_idx=0,
                cell_idx=1,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=1,
                status="quarantined",
                active=False,
            )
        )
        await db.commit()
        with pytest.raises(HTTPException, match="reserved as a storage bin home"):
            await storage_router.add_item_location(
                item.upca,
                storage_router.ItemLocationCreate(side="left_module", shelf_idx=0, cell_idx=1),
                db,
                current_user,
            )

        member_bin = StorageBin(
            barcode="900000000019",
            side="left_module",
            shelf_idx=0,
            cell_idx=2,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=1,
            status="at_home",
            active=False,
        )
        db.add(member_bin)
        await db.flush()
        db.add(StorageBinItem(bin_id=member_bin.id, item_id=item.id))
        await db.commit()
        assert await bin_storage.is_reserved_bin_location(("left_module", 0, 2), db) is True


@pytest.mark.anyio
async def test_mock_allocation_excludes_active_and_quarantined_bin_homes(
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
                    status="quarantined",
                    active=False,
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
        assert await BinStorageService(db).active_workflow() is None

        selected_bin = await storage_router.create_bin_workflow(
            storage_router.BinWorkflowCreate(
                operation="store", upca=bin_item.upca, storage_mode="bin"
            ),
            session=db,
            current_user=current_user,
        )
        assert selected_bin["storage_mode"] == "bin"
        assert selected_bin["workflow"]["item"]["id"] == bin_item.id
        assert selected_bin["workflow"]["state"] == "recovery_required"
        assert bin_row.status == "checked_out"


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
        assert await BinStorageService(db).active_workflow() is None


@pytest.mark.anyio
async def test_bin_start_waits_for_direct_enqueue_then_rejects_queued_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = BlockingWorker()
    configured = _workflow_bin("900000000016", "left_module", 0, 0)
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: _bin_config(configured))
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    direct_enqueue_held = asyncio.Event()
    admission_attempts = 0
    original_guard = bin_storage.bin_workflow_command_admission_guard

    @asynccontextmanager
    async def observed_guard():
        nonlocal admission_attempts
        admission_attempts += 1
        if admission_attempts == 2:
            direct_enqueue_held.set()
        async with original_guard():
            yield

    monkeypatch.setattr(robot_router, "bin_workflow_command_admission_guard", observed_guard)
    monkeypatch.setattr(storage_router, "bin_workflow_command_admission_guard", observed_guard)

    async with AsyncSessionLocal() as setup_db:
        await StorageService(setup_db).create_item(
            "123456789016", "Target", {"width": 10, "height": 10, "length": 10}
        )

    async with AsyncSessionLocal() as direct_db, AsyncSessionLocal() as bin_db:
        direct_task = asyncio.create_task(
            robot_router.robot_commands(
                HomeCommand(),
                worker=worker,  # type: ignore[arg-type]
                current_user=SimpleNamespace(id=1, role=SimpleNamespace(name="admin")),
                db=direct_db,
            )
        )
        await worker.command_enqueue_started.wait()
        bin_task = asyncio.create_task(
            storage_router.create_bin_workflow(
                storage_router.BinWorkflowCreate(operation="store", upca="123456789016"),
                session=bin_db,
                current_user=SimpleNamespace(id=1),
            )
        )
        await direct_enqueue_held.wait()
        worker.release_command_enqueue.set()
        await direct_task

        with pytest.raises(HTTPException) as error:
            await bin_task

        assert error.value.status_code == 409
        assert error.value.detail == "Robot has queued commands"
        assert len(worker.commands) == 1
        assert await BinStorageService(bin_db).active_workflow() is None


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
async def test_store_workflow_changes_membership_only_after_bin_returns() -> None:
    worker = FakeWorker()
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789012", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000003",
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
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None

        await service.start_retrieval(workflow, worker)  # type: ignore[arg-type]
        assert worker.commands[-1].command == "retrieve_item"
        assert worker.commands[-1].request_id == workflow.id
        assert await service.handle_robot_result(
            {
                "command": "retrieve_item",
                "message": "success",
                "request_id": workflow.id,
                "upca": bin_row.barcode,
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "awaiting_operator"

        assert await service.handle_scan(item.upca, worker)  # type: ignore[arg-type]
        assert workflow.state == "awaiting_operator"
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None

        assert await service.handle_scan(bin_row.barcode, worker)  # type: ignore[arg-type]
        assert workflow.state == "awaiting_operator"
        assert worker.commands[-1].command == "retrieve_item"

        await service.start_return(workflow, worker)  # type: ignore[arg-type]
        assert workflow.state == "returning_bin"
        assert worker.commands[-1].command == "store_item"
        assert worker.commands[-1].request_id == workflow.id
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None

        assert await service.handle_robot_result(
            {
                "command": "store_item",
                "message": "success",
                "request_id": workflow.id,
                "upca": bin_row.barcode,
                "weight": 200,
            },
            worker,  # type: ignore[arg-type]
        )
        membership = (await db.execute(select(StorageBinItem))).scalar_one()
        assert membership.item_id == item.id
        assert workflow.state == "completed"
        assert workflow.lock_key is None


@pytest.mark.anyio
async def test_resumed_scan_wait_requires_home_before_return_and_continues_after_home() -> None:
    worker = FakeWorker()
    worker.is_needs_home = True
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789020", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000020",
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
        await service.transition(workflow, "awaiting_operator")

        with pytest.raises(BinWorkflowConflict, match="Home required"):
            await service.start_return(workflow, worker)  # type: ignore[arg-type]
        assert workflow.state == "awaiting_operator"
        assert worker.commands == []

        worker.is_needs_home = False
        await service.start_return(workflow, worker)  # type: ignore[arg-type]
        assert workflow.state == "returning_bin"
        assert worker.commands[-1].command == "store_item"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "workflow_state",
    ["awaiting_operator", "recovery_required"],
)
async def test_active_resumable_workflow_allows_only_admin_home_when_required(
    workflow_state: str,
) -> None:
    worker = FakeWorker()
    worker.is_needs_home = True
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789021", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000021",
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None
        await BinStorageService(db).transition(workflow, workflow_state)

        admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
        await robot_router._ensure_no_active_bin_workflow(HomeCommand(), db, admin, worker)  # type: ignore[arg-type]

        with pytest.raises(HTTPException) as non_admin_error:
            await robot_router._ensure_no_active_bin_workflow(
                HomeCommand(),
                db,
                SimpleNamespace(role=SimpleNamespace(name="operator")),
                worker,  # type: ignore[arg-type]
            )
        assert non_admin_error.value.status_code == 423

        with pytest.raises(HTTPException) as command_error:
            await robot_router._ensure_no_active_bin_workflow(
                StoreItemCommand(item={"upca": "123"}),
                db,
                admin,
                worker,  # type: ignore[arg-type]
            )
        assert command_error.value.status_code == 423

        worker.is_needs_home = False
        with pytest.raises(HTTPException) as no_home_required_error:
            await robot_router._ensure_no_active_bin_workflow(HomeCommand(), db, admin, worker)  # type: ignore[arg-type]
        assert no_home_required_error.value.status_code == 423

        for recovery_state in ("_is_emergency_stopped", "_is_crashed"):
            setattr(worker, recovery_state, True)
            await robot_router._ensure_no_active_bin_workflow(HomeCommand(), db, admin, worker)  # type: ignore[arg-type]

            with pytest.raises(HTTPException) as non_admin_error:
                await robot_router._ensure_no_active_bin_workflow(
                    HomeCommand(),
                    db,
                    SimpleNamespace(role=SimpleNamespace(name="operator")),
                    worker,  # type: ignore[arg-type]
                )
            assert non_admin_error.value.status_code == 423
            setattr(worker, recovery_state, False)


@pytest.mark.anyio
async def test_active_bin_workflow_blocks_public_queue_and_manual_motion_but_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = MagicMock()
    worker.clear_commands = AsyncMock()
    worker.grip = AsyncMock()
    worker.release_gripper = AsyncMock()
    worker.shutdown = AsyncMock()
    worker._robot._get_motor_by_name.return_value = MagicMock()
    monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789026", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000026",
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

        mutations = (
            robot_router.clear_robot_commands(worker, db),
            robot_router.control_gripper("grip", worker, db),
            robot_router.home_motor("x_axis", worker=worker, db=db),
            robot_router.move_motor(
                "x_axis", robot_router.MotorMoveRequest(position_mm=1), worker, db
            ),
            robot_router.control_door("open", worker, db),
        )
        for mutation in mutations:
            with pytest.raises(HTTPException) as error:
                await mutation
            assert error.value.status_code == 423
            assert error.value.detail["code"] == "bin_workflow_active"
            assert error.value.detail["workflow_id"] == workflow.id

        await robot_router.stop_robot(worker)
        assert workflow.state == "awaiting_operator"

    worker.clear_commands.assert_not_awaited()
    worker.grip.assert_not_awaited()
    worker.shutdown.assert_awaited_once()


@pytest.mark.anyio
async def test_bin_motion_admission_blocks_manual_motion_and_active_demo_or_laser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    worker._demo_cycling = False

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789027", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000027",
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None
        await BinStorageService(db).transition(workflow, "awaiting_operator")
        await db.commit()

        with pytest.raises(HTTPException) as workflow_error:
            async with manual_motion_guard("calibration_move", check_bin_workflow=True):
                pytest.fail("motion must not be admitted while a bin workflow is active")
        assert workflow_error.value.status_code == 423
        assert workflow_error.value.detail["code"] == "bin_workflow_active"

    worker._demo_cycling = True
    with pytest.raises(BinWorkflowConflict, match="Demo cycle"):
        await bin_storage.ensure_worker_queue_empty_for_bin_start(worker)

    worker._demo_cycling = False
    monkeypatch.setattr(
        bin_storage, "is_laser_auto_calibration_running", AsyncMock(return_value=True)
    )
    with pytest.raises(BinWorkflowConflict, match="Laser auto calibration"):
        await bin_storage.ensure_worker_queue_empty_for_bin_start(worker)


@pytest.mark.anyio
async def test_retry_requires_successful_home_before_enqueueing_recovery_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=False)
    worker.is_needs_home = True
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789022", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000022",
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
        workflow.error = "motor fault"
        await service.transition(workflow, "recovery_required", workflow.error)
        admin = SimpleNamespace(role=SimpleNamespace(name="admin"))

        with pytest.raises(HTTPException) as home_required_error:
            await storage_router.retry_bin_workflow(workflow.id, db, admin)
        assert home_required_error.value.status_code == 409
        assert workflow.state == "recovery_required"
        assert worker.commands == []

        worker.is_needs_home = False
        result = await storage_router.retry_bin_workflow(workflow.id, db, admin)
        assert result["workflow"]["state"] == "retrieving_bin"
        assert worker.commands[-1].command == "retrieve_item"


@pytest.mark.anyio
async def test_retry_retrieval_rejects_occupied_io_box_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789044", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000044",
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
        await service.transition(workflow, "recovery_required", "motor fault")
        history_before_retry = list(workflow.history)

        with pytest.raises(HTTPException) as occupied_error:
            await storage_router.retry_bin_workflow(workflow.id, db, admin)

        assert occupied_error.value.status_code == 409
        assert (
            occupied_error.value.detail
            == "Remove the item from the IO box before retrieving the storage bin"
        )
        assert workflow.state == "recovery_required"
        assert workflow.history == history_before_retry
        assert workflow.recovery_claim is None
        assert worker.commands == []


@pytest.mark.anyio
async def test_retry_return_rejects_empty_io_box_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=False)
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789045", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000045",
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
        await service.transition(workflow, "returning_bin")
        await service.transition(workflow, "recovery_required", "motor fault")
        history_before_retry = list(workflow.history)

        with pytest.raises(HTTPException) as empty_error:
            await storage_router.retry_bin_workflow(workflow.id, db, admin)

        assert empty_error.value.status_code == 409
        assert empty_error.value.detail == "Place the bin in the IO box before storing it"
        assert workflow.state == "recovery_required"
        assert workflow.history == history_before_retry
        assert workflow.recovery_claim is None
        assert worker.commands == []


@pytest.mark.anyio
async def test_concurrent_retries_with_separate_sessions_enqueue_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_storage, "_command_admission_lock", asyncio.Lock())
    worker = BlockingWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789041", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000041",
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None
        await BinStorageService(db).transition(workflow, "recovery_required", "motor fault")
        workflow_id = workflow.id

    async def retry() -> object:
        async with AsyncSessionLocal() as db:
            try:
                return await storage_router.retry_bin_workflow(workflow_id, db, admin)
            except HTTPException as exc:
                return exc

    first = asyncio.create_task(retry())
    await worker.command_enqueue_started.wait()
    second = asyncio.create_task(retry())
    await asyncio.sleep(0)
    worker.release_command_enqueue.set()
    first_result, second_result = await asyncio.gather(first, second)

    results = [first_result, second_result]
    assert sum(isinstance(result, HTTPException) for result in results) == 1
    loser = next(result for result in results if isinstance(result, HTTPException))
    assert loser.status_code == 409
    assert len(worker.commands) == 1
    async with AsyncSessionLocal() as db:
        workflow = await db.get(StorageBinWorkflow, workflow_id)
        assert workflow is not None
        assert workflow.state == "retrieving_bin"
        assert workflow.recovery_claim is None


@pytest.mark.anyio
async def test_reconcile_claim_blocks_concurrent_retry_before_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_storage, "_command_admission_lock", asyncio.Lock())
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789042", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000042",
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None
        await BinStorageService(db).transition(workflow, "recovery_required", "motor fault")
        workflow_id = workflow.id

    reconcile_entered = asyncio.Event()
    release_reconcile = asyncio.Event()
    original_reconcile = BinStorageService.reconcile

    async def blocking_reconcile(self, *args, **kwargs):
        reconcile_entered.set()
        await release_reconcile.wait()
        return await original_reconcile(self, *args, **kwargs)

    monkeypatch.setattr(BinStorageService, "reconcile", blocking_reconcile)

    async def reconcile() -> object:
        async with AsyncSessionLocal() as db:
            try:
                return await storage_router.reconcile_bin_workflow(
                    workflow_id,
                    storage_router.BinWorkflowReconcile(
                        resolution="returned", target_item_in_bin=True
                    ),
                    db,
                    admin,
                )
            except HTTPException as exc:
                return exc

    async def retry() -> object:
        async with AsyncSessionLocal() as db:
            try:
                return await storage_router.retry_bin_workflow(workflow_id, db, admin)
            except HTTPException as exc:
                return exc

    reconciliation = asyncio.create_task(reconcile())
    await reconcile_entered.wait()
    retried = asyncio.create_task(retry())
    await asyncio.sleep(0)
    release_reconcile.set()
    reconciliation_result, retry_result = await asyncio.gather(reconciliation, retried)

    assert not isinstance(reconciliation_result, HTTPException)
    assert isinstance(retry_result, HTTPException)
    assert retry_result.status_code == 409
    assert worker.commands == []
    async with AsyncSessionLocal() as db:
        workflow = await db.get(StorageBinWorkflow, workflow_id)
        assert workflow is not None
        assert workflow.state == "reconciled"
        assert workflow.recovery_claim is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("resolution", "expected_status"),
    (("returned", "at_home"), ("quarantined", "quarantined")),
)
async def test_admin_can_reconcile_bin_while_waiting_for_return(
    monkeypatch: pytest.MonkeyPatch,
    resolution: str,
    expected_status: str,
) -> None:
    monkeypatch.setattr(storage_router.robot_core, "robot_instance", None)
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", None)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789046", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000046",
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None
        await BinStorageService(db).transition(workflow, "awaiting_operator")

        result = await storage_router.reconcile_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReconcile(
                resolution=resolution,
                target_item_in_bin=True if resolution == "returned" else None,
            ),
            db,
            admin,
        )

        assert result["workflow"]["state"] == "reconciled"
        expected_resolution = {"resolution": resolution}
        if resolution == "returned":
            expected_resolution["target_item_in_bin"] = True
        assert result["workflow"]["resolution"] == expected_resolution
        await db.refresh(bin_row)
        assert bin_row.status == expected_status
        membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
        ).scalar_one_or_none()
        assert (membership is not None) is (resolution == "returned")


@pytest.mark.anyio
async def test_scanned_unavailable_bin_reconciles_all_confirmed_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(storage_router.robot_core, "robot_instance", None)
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)

    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        first = await storage.create_item(
            "123456789048", "First", {"width": 10, "height": 10, "length": 10}
        )
        second = await storage.create_item(
            "123456789049", "Second", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000048",
            side="right_module",
            shelf_idx=1,
            cell_idx=2,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=3,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.flush()
        db.add_all(
            [
                StorageBinItem(bin_id=bin_row.id, item_id=first.id),
                StorageBinItem(bin_id=bin_row.id, item_id=second.id),
            ]
        )
        await db.commit()
        service = BinStorageService(db)
        previous = await service.begin("retrieve", first, None)
        assert previous is not None
        await service.transition(previous, "recovery_required", "bin dropped")
        await service.reconcile(
            previous,
            resolution="quarantined",
            target_item_in_bin=True,
        )

        assert await service.handle_scan(bin_row.barcode, worker)  # type: ignore[arg-type]
        workflow = await service.active_workflow()
        assert workflow is not None
        assert workflow.operation == "restore"
        assert workflow.state == "awaiting_operator"
        payload = await service.serialize_workflow(workflow)
        assert {item["id"] for item in payload["contents"]} == {first.id, second.id}
        assert all(item["confirmed"] for item in payload["contents"])

        workflow.resolution = {
            **(workflow.resolution or {}),
            "confirmed_item_ids": [second.id],
        }
        await service.transition(workflow, "awaiting_operator", "operator unchecked first item")
        assert await service.handle_scan(first.upca, worker)  # type: ignore[arg-type]
        payload = await service.serialize_workflow(workflow)
        assert {item["id"] for item in payload["contents"] if item["confirmed"]} == {
            first.id,
            second.id,
        }

        returned = await storage_router.return_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReturn(confirmed_item_ids=[first.id]),
            db,
        )
        assert returned["workflow"]["state"] == "returning_bin"
        assert worker.commands[-1].command == "store_item"
        assert await service.handle_robot_result(
            {
                "command": "store_item",
                "message": "success",
                "request_id": workflow.id,
                "upca": bin_row.barcode,
            },
            worker,  # type: ignore[arg-type]
        )

        member_ids = set(
            (
                await db.execute(
                    select(StorageBinItem.item_id).where(StorageBinItem.bin_id == bin_row.id)
                )
            ).scalars()
        )
        assert member_ids == {first.id}
        assert bin_row.status == "at_home"
        assert workflow.state == "completed"


@pytest.mark.anyio
async def test_unavailable_bin_return_can_close_before_motion() -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789050", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000050",
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
        service = BinStorageService(db)
        previous = await service.begin("retrieve", item, None)
        assert previous is not None
        await service.transition(previous, "recovery_required", "bin dropped")
        await service.reconcile(
            previous,
            resolution="quarantined",
            target_item_in_bin=True,
        )
        workflow = await service.begin_unavailable_return(bin_row.barcode)
        assert workflow is not None

        result = await storage_router.cancel_unavailable_bin_return(workflow.id, db)

        assert result["workflow"]["state"] == "reconciled"
        assert result["workflow"]["resolution"]["resolution"] == "cancelled"
        assert bin_row.status == "quarantined"
        assert await service.active_workflow() is None


@pytest.mark.anyio
async def test_marking_restore_unavailable_does_not_reconcile_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_router.robot_core, "robot_instance", None)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789052", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000052",
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

        service = BinStorageService(db)
        previous = await service.begin("retrieve", item, None)
        assert previous is not None
        await service.transition(previous, "recovery_required", "bin unavailable")
        await service.reconcile(previous, resolution="quarantined", target_item_in_bin=None)
        workflow = await service.begin_unavailable_return(bin_row.barcode)
        assert workflow is not None
        workflow.resolution = {
            **(workflow.resolution or {}),
            "confirmed_item_ids": [],
        }
        await db.commit()

        result = await storage_router.reconcile_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReconcile(resolution="quarantined"),
            db,
            admin,
        )

        membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
        ).scalar_one_or_none()
        assert membership is not None
        assert result["workflow"]["state"] == "reconciled"
        assert result["workflow"]["resolution"]["resolution"] == "quarantined"
        assert bin_row.status == "quarantined"


@pytest.mark.anyio
async def test_failed_retry_enqueue_releases_only_its_claim_and_preserves_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWorker(FakeWorker):
        def get_cached_scale_status(self):
            return SimpleNamespace(occupied=False)

        async def enqueue_command(self, command) -> None:
            raise RuntimeError("queue unavailable")

    worker = FailingWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789043", "Target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000043",
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None
        await BinStorageService(db).transition(workflow, "recovery_required", "motor fault")
        history_before_retry = list(workflow.history)

        result = await storage_router.retry_bin_workflow(workflow.id, db, admin)

        assert result["workflow"]["state"] == "recovery_required"
        assert workflow.state == "recovery_required"
        assert workflow.error == "queue unavailable"
        assert workflow.history[: len(history_before_retry)] == history_before_retry
        assert [entry["state"] for entry in workflow.history[len(history_before_retry) :]] == [
            "retrieving_bin",
            "recovery_required",
        ]
        assert workflow.history[-1]["state"] == "recovery_required"
        assert workflow.recovery_claim is None

        retry_worker = FakeWorker()
        retry_worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=False)
        monkeypatch.setattr(storage_router.robot_core, "robot_worker", retry_worker)
        retry_result = await storage_router.retry_bin_workflow(workflow.id, db, admin)
        assert retry_result["workflow"]["state"] == "retrieving_bin"
        assert len(retry_worker.commands) == 1


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
async def test_mock_workflow_accepts_optional_identity_scans_without_starting_motion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.mock)
    mock_runtime = SimpleNamespace(
        set_bin_io_box_occupied=MagicMock(),
        publish_status=AsyncMock(),
    )
    monkeypatch.setattr(storage_router, "get_mock_robot_runtime", lambda: mock_runtime)
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "ABC123", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000019",
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
        await service.transition(workflow, "awaiting_operator")

        item_scan = await storage_router.scan_mock_bin_workflow(
            workflow.id, storage_router.BinWorkflowScan(code=item.upca), db
        )
        assert item_scan["rejected"] is False
        assert workflow.state == "awaiting_operator"

        wrong_bin_scan = await storage_router.scan_mock_bin_workflow(
            workflow.id, storage_router.BinWorkflowScan(code="WRONG"), db
        )
        assert wrong_bin_scan["rejected"] is True
        assert workflow.state == "awaiting_operator"

        bin_scan = await storage_router.scan_mock_bin_workflow(
            workflow.id, storage_router.BinWorkflowScan(code=bin_row.barcode), db
        )
        assert bin_scan["rejected"] is False
        assert workflow.state == "awaiting_operator"

        returned = await storage_router.return_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReturn(target_item_in_bin=True),
            db,
        )
        assert returned["workflow"]["state"] == "completed"
        assert workflow.state == "completed"


@pytest.mark.anyio
async def test_return_endpoint_requires_occupancy_and_explicit_store_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.production)
    worker = FakeWorker()
    worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=False)
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789020", "Return target", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000020",
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

        with pytest.raises(HTTPException) as empty_box_error:
            await storage_router.return_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReturn(target_item_in_bin=True),
                db,
            )
        assert empty_box_error.value.status_code == 409
        assert workflow.state == "awaiting_operator"
        assert worker.commands == []

        worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=True)
        returned = await storage_router.return_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReturn(target_item_in_bin=True),
            db,
        )
        assert returned["workflow"]["state"] == "returning_bin"
        assert workflow.state == "returning_bin"
        assert [command.command for command in worker.commands] == ["store_item"]

        with pytest.raises(HTTPException) as duplicate_error:
            await storage_router.return_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReturn(target_item_in_bin=True),
                db,
            )
        assert duplicate_error.value.status_code == 409
        assert [command.command for command in worker.commands] == ["store_item"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "target_item_in_bin"),
    (("retrieve", False), ("store", True)),
)
async def test_interrupted_bin_motion_resumes_at_normal_return_action(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    target_item_in_bin: bool,
) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(storage_router.robot_core, "robot_instance", None)
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789025", "Interrupted target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000025",
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
        if operation == "retrieve":
            db.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await db.commit()

        service = BinStorageService(db)
        workflow = await service.begin(operation, item, None)
        assert workflow is not None
        await service.recover_interrupted_workflows()
        assert workflow.state == "recovery_required"

        returned = await storage_router.return_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReturn(target_item_in_bin=target_item_in_bin),
            db,
        )

        assert returned["workflow"]["state"] == "returning_bin"
        assert workflow.state == "returning_bin"
        assert workflow.recovery_claim is None
        assert workflow.resolution == {"target_item_in_bin": target_item_in_bin}
        assert [command.command for command in worker.commands] == ["store_item"]


@pytest.mark.anyio
async def test_interrupted_retrieval_return_requires_io_occupancy_and_releases_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(storage_router.robot_core, "robot_instance", None)
    worker = FakeWorker()
    worker.get_cached_scale_status = lambda: SimpleNamespace(occupied=False)
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789026", "Interrupted target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000026",
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

        service = BinStorageService(db)
        workflow = await service.begin("retrieve", item, None)
        assert workflow is not None
        await service.transition(workflow, "recovery_required", "emergency stop")

        with pytest.raises(HTTPException) as empty_box_error:
            await storage_router.return_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReturn(target_item_in_bin=False),
                db,
            )

        assert empty_box_error.value.status_code == 409
        assert empty_box_error.value.detail == "Place the bin in the IO box before storing it"
        assert workflow.state == "recovery_required"
        assert workflow.recovery_claim is None
        assert worker.commands == []


@pytest.mark.anyio
async def test_interrupted_return_enqueue_failure_stays_on_return_step_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(storage_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(storage_router.robot_core, "robot_instance", None)
    worker = FakeWorker()
    enqueue = AsyncMock(side_effect=RuntimeError("queue unavailable"))
    worker.enqueue_command = enqueue
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789027", "Interrupted target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000027",
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

        service = BinStorageService(db)
        workflow = await service.begin("retrieve", item, None)
        assert workflow is not None
        await service.transition(workflow, "recovery_required", "emergency stop")

        with pytest.raises(HTTPException) as enqueue_error:
            await storage_router.return_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReturn(target_item_in_bin=False),
                db,
            )

        assert enqueue_error.value.status_code == 409
        assert enqueue_error.value.detail == "queue unavailable"
        assert workflow.state == "recovery_required"
        assert workflow.error == "queue unavailable"
        assert workflow.recovery_claim is None

        enqueue.side_effect = None
        returned = await storage_router.return_bin_workflow(
            workflow.id,
            storage_router.BinWorkflowReturn(target_item_in_bin=False),
            db,
        )

        assert returned["workflow"]["state"] == "returning_bin"
        assert enqueue.await_count == 2


@pytest.mark.anyio
async def test_store_target_is_reserved_from_item_and_location_mutations() -> None:
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
        workflow = await BinStorageService(db).begin("store", item, None)
        assert workflow is not None

        with pytest.raises(HTTPException, match="reserved by an active"):
            await storage_router.create_item(
                storage_router.ItemCreate(upca=item.upca, name="Changed"), db, current_user
            )
        with pytest.raises(HTTPException, match="reserved by an active"):
            await storage_router.update_item(
                item.upca, storage_router.ItemUpdate(name="Changed"), db, current_user
            )
        with pytest.raises(HTTPException, match="reserved by an active"):
            await storage_router.delete_item(item.upca, db)
        with pytest.raises(HTTPException, match="reserved by an active"):
            await storage_router.add_item_location(
                item.upca,
                storage_router.ItemLocationCreate(side="left_module", shelf_idx=0, cell_idx=0),
                db,
                current_user,
            )
        with pytest.raises(HTTPException, match="reserved by an active"):
            await storage_router.remove_item_location(
                item.upca, "left_module", 0, 0, db, current_user
            )


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["completed", "reconciled"])
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


@pytest.mark.anyio
async def test_store_completion_revalidates_target_before_membership() -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789019", "Target", {"width": 10, "height": 10, "length": 10}
        )
        item.enrollment_weight_pending = True
        bin_row = StorageBin(
            barcode="900000000019",
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

        item.dimensions = {"width": 31, "height": 10, "length": 10}
        await db.commit()
        await service.transition(workflow, "returning_bin")
        await service.complete_return(workflow)

        await db.refresh(item)
        assert item.enrollment_weight_pending
        assert workflow.state == "recovery_required"
        assert workflow.error == "Item no longer fits the configured storage bin"
        assert bin_row.status == "checked_out"
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_store_completion_rejects_capacity_consumed_after_allocation() -> None:
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        target = await storage.create_item(
            "123456789065", "Target", {"width": 10, "height": 2, "length": 10}
        )
        existing = await storage.create_item(
            "123456789066", "Existing", {"width": 10, "height": 10, "length": 9}
        )
        bin_row = StorageBin(
            barcode="900000000065",
            side="right_module",
            shelf_idx=1,
            cell_idx=2,
            inner_dimensions={"width": 10, "height": 10, "length": 10},
            max_item_dimensions={"width": 10, "length": 10},
            max_items=3,
            status="at_home",
            active=True,
        )
        db.add(bin_row)
        await db.commit()
        service = BinStorageService(db)
        workflow = await service.begin("store", target, None)
        assert workflow is not None

        db.add(StorageBinItem(bin_id=bin_row.id, item_id=existing.id))
        await db.commit()
        await service.transition(workflow, "returning_bin")
        await service.complete_return(workflow)

        assert workflow.state == "recovery_required"
        assert workflow.error == "Storage bin does not have enough available space"
        target_membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == target.id))
        ).scalar_one_or_none()
        assert target_membership is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("operation", "target_item_in_bin", "expected_membership"),
    (("store", False, False), ("retrieve", True, True)),
)
async def test_return_confirmation_controls_target_membership(
    operation: str,
    target_item_in_bin: bool,
    expected_membership: bool,
) -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789047", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000047",
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
        if operation == "retrieve":
            db.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await db.commit()

        service = BinStorageService(db)
        workflow = await service.begin(operation, item, None)
        assert workflow is not None
        workflow.resolution = {"target_item_in_bin": target_item_in_bin}
        await service.transition(workflow, "returning_bin")
        await service.complete_return(workflow)

        membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
        ).scalar_one_or_none()
        assert (membership is not None) is expected_membership
        assert workflow.state == "completed"


@pytest.mark.anyio
async def test_post_return_validation_failure_is_durable_reconcile_only_and_cannot_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    post_return_log = MagicMock()
    monkeypatch.setattr(bin_storage.logger, "error", post_return_log)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))

    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789023", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000023",
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

        item.dimensions = {"width": 31, "height": 10, "length": 10}
        await db.commit()
        await service.transition(workflow, "returning_bin")
        assert await service.handle_robot_result(
            {
                "command": "store_item",
                "message": "success",
                "request_id": workflow.id,
                "upca": bin_row.barcode,
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "recovery_required"
        assert service.physical_return_completed(workflow)
        post_return_log.assert_called_once_with(
            "bin_workflow_post_return_failure",
            workflow_id=workflow.id,
            workflow_state="returning_bin",
            operation="store",
            bin_id=bin_row.id,
            bin_barcode=bin_row.barcode,
            item_id=item.id,
            phase="inventory_finalization",
            error="Item no longer fits the configured storage bin",
            physical_return_completed=True,
        )
        workflow_id = workflow.id

    # A new database session represents the durable state used after restart.
    async with AsyncSessionLocal() as db:
        workflow = await db.get(StorageBinWorkflow, workflow_id)
        assert workflow is not None
        assert workflow.state == "recovery_required"
        assert BinStorageService(db).physical_return_completed(workflow)
        history_before_retry = list(workflow.history)

        with pytest.raises(HTTPException) as retry_error:
            await storage_router.retry_bin_workflow(workflow.id, db, admin)

        assert retry_error.value.status_code == 409
        assert (
            retry_error.value.detail == "Physical bin return completed; workflow must be reconciled"
        )
        assert workflow.state == "recovery_required"
        assert workflow.history == history_before_retry
        assert worker.commands == []
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None

        with pytest.raises(HTTPException) as return_error:
            await storage_router.return_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReturn(target_item_in_bin=True),
                db,
            )

        assert return_error.value.status_code == 409
        assert (
            return_error.value.detail
            == "Physical bin return completed; mark the bin returned or unavailable"
        )
        assert workflow.state == "recovery_required"
        assert workflow.recovery_claim is None
        assert workflow.history == history_before_retry
        assert worker.commands == []

        bin_row = await db.get(StorageBin, workflow.bin_id)
        assert bin_row is not None
        with pytest.raises(HTTPException) as reconciliation_error:
            await storage_router.reconcile_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReconcile(resolution="returned", target_item_in_bin=True),
                db,
                admin,
            )

        assert reconciliation_error.value.status_code == 409
        assert reconciliation_error.value.detail == "Item no longer fits the configured storage bin"
        assert workflow.state == "recovery_required"
        assert workflow.error == "Item no longer fits the configured storage bin"
        assert workflow.recovery_claim is None
        assert workflow.history[-1]["state"] == "recovery_required"
        assert bin_row.status == "checked_out"
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None


@pytest.mark.anyio
@pytest.mark.parametrize("target_item_in_bin", [False, True])
async def test_returned_reconciliation_preserves_cross_bin_membership(
    target_item_in_bin: bool,
) -> None:
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789056", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bins = [
            StorageBin(
                barcode=f"90000000005{index}",
                side="right_module",
                shelf_idx=1,
                cell_idx=index,
                inner_dimensions={"width": 30, "height": 30, "length": 30},
                max_items=2,
                status="at_home",
                active=True,
            )
            for index in (6, 7)
        ]
        db.add_all(bins)
        await db.commit()

        service = BinStorageService(db)
        workflow = await service.begin("store", item, None)
        assert workflow is not None
        other_bin = next(bin_row for bin_row in bins if bin_row.id != workflow.bin_id)
        db.add(StorageBinItem(bin_id=other_bin.id, item_id=item.id))
        await db.commit()
        await service.transition(workflow, "awaiting_operator")

        with pytest.raises(HTTPException) as reconciliation_error:
            await storage_router.reconcile_bin_workflow(
                workflow.id,
                storage_router.BinWorkflowReconcile(
                    resolution="returned", target_item_in_bin=target_item_in_bin
                ),
                db,
                admin,
            )

        assert reconciliation_error.value.status_code == 409
        assert (
            reconciliation_error.value.detail
            == "Item is already associated with another storage bin"
        )
        membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
        ).scalar_one()
        assert membership.bin_id == other_bin.id
        workflow_bin = await db.get(StorageBin, workflow.bin_id)
        assert workflow_bin is not None
        assert workflow_bin.status == "checked_out"
        assert workflow.state == "recovery_required"
        assert workflow.error == "Item is already associated with another storage bin"
        assert workflow.recovery_claim is None


@pytest.mark.anyio
async def test_returned_restore_reconciliation_rejects_invalid_contents() -> None:
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789057", "Restored", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000057",
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

        service = BinStorageService(db)
        previous = await service.begin("retrieve", item, None)
        assert previous is not None
        await service.transition(previous, "recovery_required", "bin unavailable")
        await service.reconcile(previous, resolution="quarantined", target_item_in_bin=None)
        restore = await service.begin_unavailable_return(bin_row.barcode)
        assert restore is not None

        item.dimensions = {"width": 31, "height": 10, "length": 10}
        await db.commit()
        with pytest.raises(HTTPException) as reconciliation_error:
            await storage_router.reconcile_bin_workflow(
                restore.id,
                storage_router.BinWorkflowReconcile(
                    resolution="returned", confirmed_item_ids=[item.id]
                ),
                db,
                admin,
            )

        assert reconciliation_error.value.status_code == 409
        assert (
            reconciliation_error.value.detail
            == "A confirmed item no longer fits the configured storage bin"
        )
        membership = (
            await db.execute(select(StorageBinItem).where(StorageBinItem.item_id == item.id))
        ).scalar_one()
        assert membership.bin_id == bin_row.id
        assert bin_row.status == "quarantined"
        assert restore.state == "recovery_required"
        assert restore.error == "A confirmed item no longer fits the configured storage bin"
        assert restore.recovery_claim is None


@pytest.mark.anyio
async def test_return_command_failure_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = FakeWorker()
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", worker)
    admin = SimpleNamespace(role=SimpleNamespace(name="admin"))
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789024", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000024",
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
        await service.transition(workflow, "returning_bin")
        assert await service.handle_robot_result(
            {
                "command": "store_item",
                "message": "error",
                "error": "motor fault",
                "request_id": workflow.id,
                "upca": bin_row.barcode,
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "recovery_required"
        assert not service.physical_return_completed(workflow)

        result = await storage_router.retry_bin_workflow(
            workflow.id,
            db,
            admin,
            target_item_in_bin=False,
        )

        assert result["workflow"]["state"] == "returning_bin"
        assert result["workflow"]["resolution"] == {"target_item_in_bin": False}
        assert worker.commands[-1].command == "store_item"


@pytest.mark.anyio
async def test_correlated_retrieval_failure_requires_bin_workflow_recovery() -> None:
    worker = FakeWorker()
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789013", "Target", {"width": 10, "height": 10, "length": 10}
        )
        bin_row = StorageBin(
            barcode="900000000005",
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

        assert await service.handle_robot_result(
            {
                "command": "retrieve_item",
                "message": "error",
                "error": "motor fault",
                "request_id": workflow.id,
                "upca": bin_row.barcode,
                "location": {"side": "right_module", "shelf_idx": 1, "cell_idx": 2},
            },
            worker,  # type: ignore[arg-type]
        )
        assert workflow.state == "recovery_required"
        assert workflow.error == "motor fault"
        assert workflow.lock_key == "global"
        assert bin_row.status == "checked_out"
        assert (await db.execute(select(StorageBinItem))).scalar_one_or_none() is None


@pytest.mark.anyio
async def test_wrong_scan_does_not_advance_and_global_lock_is_exclusive() -> None:
    worker = FakeWorker()
    async with AsyncSessionLocal() as db:
        storage = StorageService(db)
        first_item = await storage.create_item(
            "333333333333", "First", {"width": 10, "height": 10, "length": 10}
        )
        second_item = await storage.create_item(
            "444444444444", "Second", {"width": 10, "height": 10, "length": 10}
        )
        db.add(
            StorageBin(
                barcode="900000000004",
                side="left_module",
                shelf_idx=2,
                cell_idx=0,
                inner_dimensions={"width": 20, "height": 20, "length": 20},
                max_items=3,
                status="at_home",
                active=True,
            )
        )
        await db.commit()
        service = BinStorageService(db)
        workflow = await service.begin("store", first_item, None)
        assert workflow is not None
        await service.transition(workflow, "awaiting_operator")

        assert await service.handle_scan("000000000000", worker)  # type: ignore[arg-type]
        assert workflow.state == "awaiting_operator"
        assert worker.events[-1]["kind"] == "bin_workflow_scan_rejected"

        with pytest.raises(BinWorkflowConflict):
            await service.begin("store", second_item, None)


@pytest.mark.anyio
@pytest.mark.parametrize("measured", [425.5, 0, None, "bad", float("nan"), float("inf"), -1, True])
async def test_bin_return_replaces_aggregate_without_changing_container_mass(measured) -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789081", "Opened bottle", {"width": 10, "height": 10, "length": 10}
        )
        item.weight = 350
        item.tare_weight = 100
        item.full_net_weight = 500
        item.enrollment_weight_pending = True
        bin_row = StorageBin(
            barcode="900000000081", side="right_module", shelf_idx=1, cell_idx=2,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2, status="at_home", active=True, gross_weight=200,
        )
        db.add(bin_row)
        await db.commit()
        service = BinStorageService(db)
        workflow = await service.begin("store", item, None)
        assert workflow is not None
        assert bin_row.gross_weight is None
        assert item.enrollment_weight_pending
        await service.transition(workflow, "returning_bin")
        await service.complete_return(workflow, measured)
        expected = measured if type(measured) in {int, float} and measured in {425.5, 0} else None
        assert bin_row.gross_weight == expected
        await db.refresh(item)
        assert (item.weight, item.tare_weight, item.full_net_weight) == (350, 100, 500)
        assert not item.enrollment_weight_pending
        assert item.enrollment_storage_completed
        # A replay must not replace the last successful aggregate measurement.
        await service.complete_return(workflow, 999)
        assert bin_row.gross_weight == expected


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["store", "retrieve", "restore"])
async def test_unmeasured_bin_reconciliation_clears_aggregate_and_preserves_item_mass(operation) -> None:
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            "123456789082", "Opened bottle", {"width": 10, "height": 10, "length": 10}
        )
        item.weight = 350
        item.tare_weight = 100
        item.full_net_weight = 500
        item.enrollment_weight_pending = True
        bin_row = StorageBin(
            barcode="900000000082", side="right_module", shelf_idx=1, cell_idx=2,
            inner_dimensions={"width": 30, "height": 30, "length": 30},
            max_items=2, status="checked_out", active=True, gross_weight=200,
        )
        db.add(bin_row)
        await db.flush()
        workflow = StorageBinWorkflow(
            id="unmeasured-return", operation=operation, state="recovery_required",
            lock_key="global", bin_id=bin_row.id, item_id=item.id,
            resolution={"candidate_item_ids": [item.id], "confirmed_item_ids": [item.id]}
            if operation == "restore" else None,
        )
        db.add(workflow)
        if operation == "retrieve":
            db.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await db.commit()
        await BinStorageService(db).reconcile(
            workflow, resolution="returned", target_item_in_bin=operation != "retrieve"
        )
        await db.refresh(item)
        assert bin_row.gross_weight is None
        assert (item.weight, item.tare_weight, item.full_net_weight) == (350, 100, 500)
        assert item.enrollment_weight_pending is (operation == "retrieve")

@pytest.mark.anyio
@pytest.mark.parametrize('operation', ['store', 'retrieve'])
async def test_mock_done_finishes_without_scans_and_updates_membership(monkeypatch, operation):
    monkeypatch.setattr(storage_router, 'get_operating_mode', lambda: OperatingMode.mock)
    runtime = SimpleNamespace(set_bin_io_box_occupied=MagicMock(), publish_status=AsyncMock())
    monkeypatch.setattr(storage_router, 'get_mock_robot_runtime', lambda: runtime)
    async with AsyncSessionLocal() as db:
        item = await StorageService(db).create_item(
            '123456789001', 'Tray item', {'width': 10, 'height': 10, 'length': 10}
        )
        tray = StorageBin(barcode='900000000001', side='left_module', shelf_idx=0, cell_idx=0,
                          inner_dimensions={'width': 30, 'height': 30, 'length': 30}, max_items=2,
                          status='at_home', active=True)
        db.add(tray)
        await db.flush()
        if operation == 'retrieve':
            db.add(StorageBinItem(bin_id=tray.id, item_id=item.id))
        await db.commit()
        service = BinStorageService(db)
        workflow = await service.begin(operation, item, None)
        assert workflow is not None
        await service.transition(workflow, 'awaiting_operator')
        result = await storage_router.return_bin_workflow(
            workflow.id, storage_router.BinWorkflowReturn(target_item_in_bin=operation == 'store'), db
        )
        assert result['workflow']['state'] == 'completed'
        membership = await service.item_bin(item.id)
        assert (membership is not None) == (operation == 'store')
        assert tray.status == 'at_home'
        runtime.set_bin_io_box_occupied.assert_called_once_with(False)
