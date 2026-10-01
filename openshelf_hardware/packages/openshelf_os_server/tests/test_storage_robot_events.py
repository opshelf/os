import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

import openshelf_os_server.core.robot as robot_core
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core import mock_robot as mock_core
from openshelf_os_server.core.bin_storage import BinStorageService
from openshelf_os_server.core.mock_robot import MockRobotRuntime
import openshelf_os_server.core.robot_events as robot_events_module
from openshelf_os_server.core.robot_events import (
    EventProcessingTracker,
    RobotEventListener,
    process_robot_event,
)
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.validation import ValidationError
from openshelf_os_server.database.models import (
    Item,
    ItemLocation,
    MockRobotCommand,
    StorageBin,
    StorageBinItem,
)
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router
from openshelf_os.operating_mode import OperatingMode


async def _seed_item_with_location(barcode: str, side: str, shelf_idx: int, cell_idx: int) -> None:
    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            barcode=barcode,
            name="Test Item",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=None,
            metadata={"name": "Test Item"},
        )
        await storage.add_item_location(barcode, side, shelf_idx, cell_idx)


@pytest.mark.anyio
async def test_process_robot_event_removes_location_on_success() -> None:
    await _seed_item_with_location(barcode="111", side="front_module", shelf_idx=1, cell_idx=2)

    payload = {
        "command": "retrieve_item",
        "message": "success",
        "location": {"side": "front_module", "shelf_idx": 1, "cell_idx": 2},
        "barcode": "111",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.barcode == "111")
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.anyio
async def test_process_robot_event_ignores_incomplete_payload() -> None:
    await _seed_item_with_location(barcode="222", side="front_module", shelf_idx=0, cell_idx=0)

    payload = {
        "command": "retrieve_item",
        "message": "error",
        "location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        "barcode": "222",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.barcode == "222")
        )
        assert result.scalar_one_or_none() is not None


@pytest.mark.anyio
async def test_process_robot_event_adds_location_on_store_success() -> None:
    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            barcode="333",
            name="Store Target",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=None,
            metadata={"name": "Store Target"},
        )

    payload = {
        "command": "store_item",
        "message": "success",
        "location": {"side": "front_module", "shelf_idx": 2, "cell_idx": 4},
        "barcode": "333",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.barcode == "333")
        )
        location = result.scalar_one_or_none()
        assert location is not None
        assert location.shelf_idx == 2
        assert location.cell_idx == 4


@pytest.mark.anyio
async def test_process_robot_event_creates_item_when_missing() -> None:
    payload = {
        "command": "store_item",
        "message": "success",
        "location": {"side": "front_module", "shelf_idx": 1, "cell_idx": 1},
        "barcode": "444",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        item_result = await session.execute(select(Item).where(Item.barcode == "444"))
        item = item_result.scalar_one_or_none()
        assert item is not None

        location_result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.barcode == "444")
        )
        location = location_result.scalar_one_or_none()
        assert location is not None


@pytest.mark.anyio
async def test_process_robot_event_updates_weight_from_scale() -> None:
    barcode = "555"

    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            barcode=barcode,
            name="Has Weight",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=50.0,
            metadata={"name": "Has Weight"},
        )

    payload = {
        "command": "store_item",
        "message": "success",
        "location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        "barcode": barcode,
        "weight": 152.0,
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        item_result = await session.execute(select(Item).where(Item.barcode == barcode))
        item = item_result.scalar_one_or_none()

        assert item is not None
        assert item.weight == 152.0


@pytest.mark.anyio
async def test_mock_debug_scanner_publishes_outside_tray_without_motion(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    runtime = MockRobotRuntime()
    events = await runtime.subscribe_events()
    monkeypatch.setattr(mock_core, "mock_robot_runtime", runtime)
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.mock)
    monkeypatch.setattr(
        robot_router,
        "get_settings",
        lambda: SimpleNamespace(robot=SimpleNamespace(enabled=False)),
    )

    async with AsyncSessionLocal() as session:
        item = await StorageService(session).create_item(
            barcode="scanner-restore-item",
            name="Scanner restore item",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
        )
        bin_row = StorageBin(
            barcode="scanner-restore-bin",
            side="front_module",
            shelf_idx=1,
            cell_idx=1,
            inner_dimensions={"width": 30.0, "height": 30.0, "length": 30.0},
            max_items=2,
            status="at_home",
            active=True,
        )
        session.add(bin_row)
        await session.flush()
        session.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await session.commit()

        service = BinStorageService(session)
        checkout = await service.begin("retrieve", item, None)
        assert checkout is not None
        await service.transition(checkout, "awaiting_operator")
        await service.close(checkout)

    async with AsyncSessionLocal() as session:
        commands_before_scan = len((await session.execute(select(MockRobotCommand))).scalars().all())

    response = await client.post(
        "/api/robot/debug/scanner/scan",
        json={"barcode": "scanner-restore-bin"},
    )
    assert response.status_code == 200
    assert response.json() == {"message": "success"}

    event = json.loads(await asyncio.wait_for(events.get(), timeout=1))
    assert event["kind"] == "tray_scanned"
    assert event["payload"] == "scanner-restore-bin"
    async with AsyncSessionLocal() as session:
        assert await BinStorageService(session).active_workflows() == []
        assert len((await session.execute(select(MockRobotCommand))).scalars().all()) == commands_before_scan

@pytest.mark.anyio
async def test_mock_debug_scanner_handles_active_item_and_bin_scans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = MockRobotRuntime()
    monkeypatch.setattr(mock_core, "mock_robot_runtime", runtime)
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.mock)

    async with AsyncSessionLocal() as session:
        item = await StorageService(session).create_item(
            barcode="scanner-active-item",
            name="Scanner active item",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
        )
        bin_row = StorageBin(
            barcode="scanner-active-bin",
            side="front_module",
            shelf_idx=1,
            cell_idx=1,
            inner_dimensions={"width": 30.0, "height": 30.0, "length": 30.0},
            max_items=2,
            status="at_home",
            active=True,
        )
        session.add(bin_row)
        await session.flush()
        session.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id))
        await session.commit()

        workflow = await BinStorageService(session).begin("retrieve", item, None)
        assert workflow is not None
        await BinStorageService(session).transition(workflow, "awaiting_operator")

    async with AsyncSessionLocal() as session:
        commands_before_scans = len((await session.execute(select(MockRobotCommand))).scalars().all())

    await robot_router.emit_scanner_scan_event(
        robot_router.LabelScanRequest(barcode="scanner-active-item"),
        None,
    )
    async with AsyncSessionLocal() as session:
        workflow = (await BinStorageService(session).active_workflows())[0]
        assert workflow is not None
        assert workflow.state == "awaiting_operator"

    await robot_router.emit_scanner_scan_event(
        robot_router.LabelScanRequest(barcode="scanner-active-bin"),
        None,
    )
    async with AsyncSessionLocal() as session:
        workflow = (await BinStorageService(session).active_workflows())[0]
        assert workflow is not None
        assert workflow.state == "awaiting_operator"
        assert len((await session.execute(select(MockRobotCommand))).scalars().all()) == commands_before_scans


@pytest.mark.anyio
async def test_debug_scanner_uses_worker_publish_outside_mock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Worker:
        def __init__(self) -> None:
            self.events: list[dict[str, str]] = []

        async def publish_event(self, payload: dict[str, str]) -> None:
            self.events.append(payload)

    worker = Worker()
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)

    await robot_router.emit_scanner_scan_event(
        robot_router.LabelScanRequest(barcode="production-scan"),
        worker,  # type: ignore[arg-type]
    )

    assert worker.events == [{"kind": "label_scanned", "payload": "production-scan"}]


@pytest.mark.anyio
async def test_debug_scanner_requires_hardware_enabled_outside_mock(
    monkeypatch: pytest.MonkeyPatch,
    client: AsyncClient,
) -> None:
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
    monkeypatch.setattr(
        robot_router,
        "get_settings",
        lambda: SimpleNamespace(robot=SimpleNamespace(enabled=False)),
    )

    response = await client.post(
        "/api/robot/debug/scanner/scan",
        json={"barcode": "production-scan"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "Robot is not enabled or configured"


async def _seed_active_bin_workflow(state: str) -> tuple[str, str]:
    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        item = await storage.create_item(
            barcode=f"target-{state}",
            name="Bin workflow target",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
        )
        bin_row = StorageBin(
            barcode=f"9000000000{state[-2:]}",
            side="front_module",
            shelf_idx=3,
            cell_idx=3,
            inner_dimensions={"width": 30.0, "height": 30.0, "length": 30.0},
            max_items=2,
            status="at_home",
            active=True,
        )
        session.add(bin_row)
        await session.commit()

        workflow = await BinStorageService(session).begin("store", item, None)
        assert workflow is not None
        if state == "returning_bin":
            await BinStorageService(session).transition(workflow, "returning_bin")
        return workflow.id, bin_row.barcode


@pytest.mark.anyio
async def test_shutdown_drains_completed_return_before_unsubscribing_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_id, barcode = await _seed_active_bin_workflow("returning_bin")

    class ShutdownWorker:
        def __init__(self) -> None:
            self._subscribers: list[asyncio.Queue[str]] = []
            self.events: list[str] = []

        async def subscribe_events(self) -> asyncio.Queue[str]:
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._subscribers.append(queue)
            return queue

        async def unsubscribe_events(self, queue: asyncio.Queue[str]) -> None:
            self.events.append("unsubscribe")
            self._subscribers.remove(queue)

        async def shutdown(self) -> None:
            self.events.append("shutdown")
            payload = json.dumps(
                {
                    "command": "store_item",
                    "message": "success",
                    "request_id": workflow_id,
                    "barcode": barcode,
                    "weight": 200.0,
                }
            )
            for queue in list(self._subscribers):
                await queue.put(payload)

    worker = ShutdownWorker()
    listener = RobotEventListener(worker)  # type: ignore[arg-type]
    await listener.start()
    monkeypatch.setattr(robot_core, "robot_worker", worker)
    monkeypatch.setattr(robot_core, "robot_instance", object())
    monkeypatch.setattr(robot_core, "event_listener", listener)

    await robot_core.stop_robot_worker()

    assert worker.events == ["shutdown", "unsubscribe"]
    async with AsyncSessionLocal() as session:
        workflow = await session.get(bin_storage.StorageBinWorkflow, workflow_id)
        assert workflow is not None
        assert workflow.state == "completed"
        assert (workflow.resolution or {}).get("physical_return_completed")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "command"),
    (("retrieving_bin", "retrieve_item"), ("returning_bin", "store_item")),
)
async def test_correlated_bin_event_handler_exception_never_uses_legacy_mutation(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    command: str,
) -> None:
    workflow_id, barcode = await _seed_active_bin_workflow(state)
    monkeypatch.setattr(
        BinStorageService,
        "handle_robot_result",
        AsyncMock(side_effect=RuntimeError("bin handler failed")),
    )

    if command == "retrieve_item":
        await _seed_item_with_location(barcode, "front_module", 3, 3)

    await process_robot_event(
        {
            "command": command,
            "message": "success",
            "request_id": workflow_id,
            "barcode": barcode,
            "location": {"side": "front_module", "shelf_idx": 3, "cell_idx": 3},
            "weight": 200.0,
        },
        worker=object(),  # type: ignore[arg-type]
    )

    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(select(Item).where(Item.barcode == barcode))
        ).scalar_one_or_none()
        if command == "retrieve_item":
            assert item is not None
            location = (
                await session.execute(select(ItemLocation).where(ItemLocation.item_id == item.id))
            ).scalar_one_or_none()
            assert location is not None
        else:
            assert item is None


@pytest.mark.anyio
@pytest.mark.parametrize("reservation", ("configured", "durable", "outside"))
async def test_reserved_bin_barcode_and_home_never_use_legacy_mutation_without_workflow(
    monkeypatch: pytest.MonkeyPatch,
    reservation: str,
) -> None:
    barcode = f"9000000000{len(reservation):02d}"
    home = ("back_module", 2, len(reservation))
    if reservation == "configured":
        monkeypatch.setattr(
            bin_storage,
            "get_root_config",
            lambda: SimpleNamespace(
                storage=SimpleNamespace(
                    bins=[
                        SimpleNamespace(
                            barcode=barcode,
                            home_location=SimpleNamespace(
                                side=home[0], shelf_idx=home[1], cell_idx=home[2]
                            ),
                        )
                    ]
                )
            ),
        )
    else:
        async with AsyncSessionLocal() as session:
            session.add(
                StorageBin(
                    barcode=barcode,
                    side=home[0],
                    shelf_idx=home[1],
                    cell_idx=home[2],
                    inner_dimensions={"width": 30.0, "height": 30.0, "length": 30.0},
                    max_items=2,
                    status="outside" if reservation == "outside" else "at_home",
                    active=True,
                )
            )
            await session.commit()

    for barcode, location in (
        (barcode, {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}),
        (f"direct-{reservation}", {"side": home[0], "shelf_idx": home[1], "cell_idx": home[2]}),
    ):
        await process_robot_event(
            {"command": "store_item", "message": "success", "barcode": barcode, "location": location}
        )

    async with AsyncSessionLocal() as session:
        items = (
            (
                await session.execute(
                    select(Item).where(Item.barcode.in_([barcode, f"direct-{reservation}"]))
                )
            )
            .scalars()
            .all()
        )
        assert items == []


@pytest.mark.anyio
async def test_released_bin_barcode_and_home_allow_legacy_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barcode = "900000000010"
    home = ("back_module", 2, 10)
    async with AsyncSessionLocal() as session:
        session.add(
            StorageBin(
                barcode=barcode,
                side=home[0],
                shelf_idx=home[1],
                cell_idx=home[2],
                inner_dimensions={"width": 30.0, "height": 30.0, "length": 30.0},
                max_items=2,
                status="outside",
                active=False,
            )
        )
        await session.commit()

    for barcode, location in (
        (barcode, {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}),
        ("direct-released", {"side": home[0], "shelf_idx": home[1], "cell_idx": home[2]}),
    ):
        await process_robot_event(
            {"command": "store_item", "message": "success", "barcode": barcode, "location": location}
        )

    async with AsyncSessionLocal() as session:
        items = (
            (
                await session.execute(
                    select(Item).where(Item.barcode.in_([barcode, "direct-released"]))
                )
            )
            .scalars()
            .all()
        )
        assert {item.barcode for item in items} == {barcode, "direct-released"}


@pytest.mark.anyio
async def test_unrelated_direct_event_is_not_swallowed_by_active_bin_workflow() -> None:
    await _seed_active_bin_workflow("retrieving_bin")

    await process_robot_event(
        {
            "command": "store_item",
            "message": "success",
            "barcode": "ordinary-direct",
            "location": {"side": "front_module", "shelf_idx": 4, "cell_idx": 4},
        },
        worker=object(),  # type: ignore[arg-type]
    )

    async with AsyncSessionLocal() as session:
        item = (
            await session.execute(select(Item).where(Item.barcode == "ordinary-direct"))
        ).scalar_one_or_none()
        assert item is not None


@pytest.mark.anyio
async def test_tracker_inactive_waits_nothing() -> None:
    tracker = EventProcessingTracker()
    assert await tracker.wait_until_processed(1, timeout_s=0.05) is True


@pytest.mark.anyio
async def test_tracker_waits_until_recorded() -> None:
    tracker = EventProcessingTracker()
    tracker.activate()

    observed: dict[str, bool] = {}

    async def waiter() -> None:
        observed["done"] = await tracker.wait_until_processed(2, timeout_s=5.0)

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    await tracker.record_processed()
    assert observed.get("done") is None
    await tracker.record_processed()
    await task
    assert observed["done"] is True


@pytest.mark.anyio
async def test_tracker_wait_times_out_when_unprocessed() -> None:
    tracker = EventProcessingTracker()
    tracker.activate()
    assert await tracker.wait_until_processed(1, timeout_s=0.05) is False


@pytest.mark.anyio
async def test_listener_records_processed_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh_tracker = EventProcessingTracker()
    monkeypatch.setattr(robot_events_module, "event_processing_tracker", fresh_tracker)

    class ListenerWorker:
        def __init__(self) -> None:
            self.queues: list[asyncio.Queue[str]] = []

        async def subscribe_events(self) -> asyncio.Queue[str]:
            queue: asyncio.Queue[str] = asyncio.Queue()
            self.queues.append(queue)
            return queue

        async def unsubscribe_events(self, queue: asyncio.Queue[str]) -> None:
            self.queues.remove(queue)

    worker = ListenerWorker()
    listener = RobotEventListener(worker)  # type: ignore[arg-type]
    await listener.start()
    try:
        queue = worker.queues[0]
        await queue.put(
            json.dumps({"command": "retrieve_item", "message": "skipped", "barcode": "x"})
        )
        assert await fresh_tracker.wait_until_processed(1, timeout_s=2.0) is True
    finally:
        await listener.stop()


@pytest.mark.anyio
async def test_retrieve_commit_pushes_fresh_status() -> None:
    await _seed_item_with_location(barcode="781", side="front_module", shelf_idx=2, cell_idx=3)

    snapshots: list[bool] = []

    class StatusWorker:
        async def get_status_snapshot(self, force_refresh: bool = False) -> str:
            snapshots.append(force_refresh)
            return "{}"

    await process_robot_event(
        {
            "command": "retrieve_item",
            "message": "success",
            "barcode": "781",
            "location": {"side": "front_module", "shelf_idx": 2, "cell_idx": 3},
        },
        worker=StatusWorker(),  # type: ignore[arg-type]
    )

    assert snapshots == [True]


@pytest.mark.anyio
async def test_store_commit_pushes_fresh_status() -> None:
    snapshots: list[bool] = []

    class StatusWorker:
        async def get_status_snapshot(self, force_refresh: bool = False) -> str:
            snapshots.append(force_refresh)
            return "{}"

    await process_robot_event(
        {
            "command": "store_item",
            "message": "success",
            "barcode": "782",
            "location": {"side": "back_module", "shelf_idx": 1, "cell_idx": 4},
            "weight": 250.0,
        },
        worker=StatusWorker(),  # type: ignore[arg-type]
    )

    assert snapshots == [True]


@pytest.mark.anyio
async def test_retrieve_without_removal_does_not_push_status() -> None:
    snapshots: list[bool] = []

    class StatusWorker:
        async def get_status_snapshot(self, force_refresh: bool = False) -> str:
            snapshots.append(force_refresh)
            return "{}"

    await process_robot_event(
        {
            "command": "retrieve_item",
            "message": "success",
            "barcode": "no-such-barcode",
            "location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        },
        worker=StatusWorker(),  # type: ignore[arg-type]
    )

    assert snapshots == []
