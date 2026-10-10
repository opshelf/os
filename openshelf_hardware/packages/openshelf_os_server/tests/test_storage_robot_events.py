import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import openshelf_os_server.core.robot as robot_core
import openshelf_os_server.core.robot_events as robot_events
from openshelf_os_server.core import bin_storage
from openshelf_os_server.core.bin_storage import BinStorageService
from openshelf_os_server.core.event_log import latest_robot_event_seq, read_robot_events
from openshelf_os_server.core.robot_events import (
    RobotEventListener,
    process_robot_event,
)
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.models import (
    Item,
    ItemLocation,
    StorageBin,
)
from openshelf_os_server.database.session import AsyncSessionLocal


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



class _FanoutWorker:
    """Fans each published event out to every subscriber, like RobotWorker."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []

    async def subscribe_events(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    async def unsubscribe_events(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.remove(queue)

    async def publish_event(self, payload: dict[str, str]) -> None:
        for queue in list(self._subscribers):
            await queue.put(json.dumps(payload))


@pytest.mark.anyio
async def test_late_event_subscriber_waits_for_the_inventory_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A kiosk reload opens its event stream long after the API started. Its first event
    # must still wait for the listener's commit, or its inventory refresh reads the old cell.
    # The stream forwards only logged events, so the log row must wait for that commit.
    release = asyncio.Event()
    processed: list[str] = []

    async def process(payload: dict[str, str], worker: object) -> None:
        if payload["barcode"] == "late":
            await release.wait()
        processed.append(payload["barcode"])

    monkeypatch.setattr(robot_events, "process_robot_event", process)
    worker = _FanoutWorker()
    listener = RobotEventListener(worker)  # type: ignore[arg-type]
    await listener.start()
    try:
        await worker.publish_event({"barcode": "early"})
        await listener.drain()

        await worker.subscribe_events()
        top = await latest_robot_event_seq()
        await worker.publish_event({"barcode": "late"})

        await asyncio.sleep(0.05)
        assert await read_robot_events(top, 10) == []
        release.set()
        await listener.drain()
        assert [row.payload["barcode"] for row in await read_robot_events(top, 10)] == ["late"]
        assert processed == ["early", "late"]
    finally:
        await listener.stop()


@pytest.mark.anyio
async def test_event_listener_survives_a_failing_event(monkeypatch: pytest.MonkeyPatch) -> None:
    processed: list[str] = []

    async def process(payload: dict[str, str], worker: object) -> None:
        if payload["barcode"] == "broken":
            raise RuntimeError("database is locked")
        processed.append(payload["barcode"])

    monkeypatch.setattr(robot_events, "process_robot_event", process)
    worker = _FanoutWorker()
    listener = RobotEventListener(worker)  # type: ignore[arg-type]
    await listener.start()
    try:
        top = await latest_robot_event_seq()
        await worker.publish_event({"barcode": "broken"})
        await worker.publish_event({"barcode": "next"})
        await listener.drain()

        assert processed == ["next"]
        # The failed event is still logged, so stream clients get its command result.
        logged = [row.payload["barcode"] for row in await read_robot_events(top, 10)]
        assert logged == ["broken", "next"]
    finally:
        await listener.stop()
