from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from openshelf_os.operating_mode import OperatingMode
from openshelf_os_server.core import mock_robot as mock_core
from openshelf_os_server.core import robot as robot_core
from openshelf_os_server.core.mock_robot import MockRobotRuntime
from openshelf_os_server.core.robot_events import RobotEventListener
from openshelf_os_server.database.models import Item, ItemLocation, MockRobotCommand, RobotEvent
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.main import app
from openshelf_os_server.routers import robot as robot_router


@pytest.fixture
async def mock_runtime(monkeypatch: pytest.MonkeyPatch) -> MockRobotRuntime:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(MockRobotCommand))
        await session.execute(delete(RobotEvent))
        await session.execute(delete(ItemLocation))
        await session.execute(delete(Item))
        await session.commit()

    runtime = MockRobotRuntime(execution_seconds=0.01)
    await runtime.start()
    monkeypatch.setattr(mock_core, "mock_robot_runtime", runtime)
    monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.mock)
    try:
        yield runtime
    finally:
        await runtime.stop()
        mock_core.mock_robot_runtime = None
        async with AsyncSessionLocal() as session:
            await session.execute(delete(MockRobotCommand).where(MockRobotCommand.runtime == "mock"))
            await session.execute(delete(RobotEvent))
            await session.execute(delete(ItemLocation))
            await session.execute(delete(Item))
            await session.commit()


async def _run_command(client: AsyncClient, payload: dict) -> str:
    response = await client.post("/api/robot/commands", json=payload)
    assert response.status_code == 200, response.text
    command_id = response.json()["command_id"]
    for _ in range(500):
        status_response = await client.get(f"/api/robot/commands/{command_id}")
        if status_response.json()["status"] in {"succeeded", "failed"}:
            return command_id
        await asyncio.sleep(0.01)
    raise AssertionError(f"command {command_id} did not reach a terminal status")


async def _event_rows() -> list[RobotEvent]:
    async with AsyncSessionLocal() as session:
        return list(
            (await session.execute(select(RobotEvent).order_by(RobotEvent.seq))).scalars()
        )


class SseStream:
    """Drive one SSE route of the app directly, collecting raw ASGI frames.

    httpx's ASGITransport buffers whole responses before returning, so a
    long-lived stream can only be observed by running the ASGI app directly
    (the same approach `test_stream_db_scope.py` uses).
    """

    def __init__(self, path: str, auth_headers: dict[str, str], extra: dict[str, str] | None = None) -> None:
        self.sent: list[dict] = []
        self.disconnected = asyncio.Event()
        headers = {"authorization": auth_headers["Authorization"]}
        if extra:
            headers.update({key.lower(): value for key, value in extra.items()})
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
        }
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        body_served = False
        disconnected = self.disconnected

        async def receive() -> dict:
            nonlocal body_served
            if not body_served:
                body_served = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            self.sent.append(message)

        self.task = asyncio.create_task(app(self.scope, receive, send))
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            if any(m.get("type") == "http.response.start" for m in self.sent):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("stream did not start (no response.start)")

    def frames(self) -> list[tuple[int | None, str]]:
        """(seq, data) pairs parsed from the accumulated SSE body bytes."""
        raw = b"".join(
            m.get("body", b"") for m in self.sent if m.get("type") == "http.response.body"
        )
        parsed = []
        for chunk in raw.decode("utf-8").split("\r\n\r\n"):
            seq = None
            data = None
            for line in chunk.split("\r\n"):
                if line.startswith("id:"):
                    try:
                        seq = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.startswith("data:"):
                    data = line.split(":", 1)[1].strip()
            if data is not None:
                parsed.append((seq, data))
        return parsed

    async def wait_for_frame(self, predicate, timeout: float = 10.0) -> tuple[int | None, str]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for frame in self.frames():
                if predicate(frame):
                    return frame
            if self.task is not None and self.task.done():
                try:
                    exc = self.task.exception()
                except (asyncio.CancelledError, StopIteration):
                    exc = "cancelled"
                raise AssertionError(
                    f"stream task ended before a frame matched; exc={exc!r}; messages={self.sent}"
                )
            await asyncio.sleep(0.01)
        raise AssertionError(f"no frame matched within {timeout}s; got {self.frames()}")

    async def stop(self) -> None:
        self.disconnected.set()
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


@pytest.mark.anyio
async def test_mock_events_are_logged_with_monotonic_seq(
    client: AsyncClient,
    mock_runtime: MockRobotRuntime,
) -> None:
    store_id = await _run_command(
        client,
        {
            "command": "store_item",
            "item": {
                "barcode": "036000291452",
                "name": "Logged",
                "dimensions": {"width": 10, "height": 20, "length": 30},
            },
        },
    )
    await _run_command(
        client,
        {
            "command": "retrieve_item",
            "location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        },
    )

    rows = await _event_rows()
    assert len(rows) == 2
    assert rows[0].seq + 1 == rows[1].seq
    assert rows[0].command_id == store_id
    assert rows[0].payload["message"] == "success"
    assert rows[1].payload["command"] == "retrieve_item"


@pytest.mark.anyio
async def test_event_log_endpoint_replays_and_pages(
    client: AsyncClient,
    mock_runtime: MockRobotRuntime,
) -> None:
    for index in range(3):
        await _run_command(
            client,
            {
                "command": "store_item",
                "item": {
                    "barcode": f"0360002914{index}",
                    "name": f"Page {index}",
                    "dimensions": {"width": 10, "height": 20, "length": 30},
                },
            },
        )
    rows = await _event_rows()

    all_response = await client.get("/api/robot/events/log", params={"since": 0, "limit": 10})
    assert all_response.status_code == 200
    body = all_response.json()
    assert [event["seq"] for event in body["events"]] == [row.seq for row in rows]
    assert body["latest_seq"] == rows[-1].seq
    assert body["next_since"] == rows[-1].seq

    first_seq = rows[0].seq
    tail_response = await client.get(
        "/api/robot/events/log", params={"since": first_seq, "limit": 10}
    )
    tail = tail_response.json()
    assert [event["seq"] for event in tail["events"]] == [row.seq for row in rows[1:]]
    assert tail["next_since"] == rows[-1].seq

    page = await client.get("/api/robot/events/log", params={"since": 0, "limit": 1})
    page_body = page.json()
    assert len(page_body["events"]) == 1
    assert page_body["next_since"] == rows[0].seq
    assert page_body["next_since"] < page_body["latest_seq"]

    beyond = await client.get(
        "/api/robot/events/log", params={"since": rows[-1].seq}
    )
    beyond_body = beyond.json()
    assert beyond_body["events"] == []
    assert beyond_body["next_since"] == rows[-1].seq


@pytest.mark.anyio
async def test_event_log_endpoint_requires_authentication(
    unauthenticated_client: AsyncClient,
) -> None:
    response = await unauthenticated_client.get("/api/robot/events/log")
    assert response.status_code == 401


@pytest.mark.anyio
async def test_sse_stream_carries_seq_ids_and_resumes_with_last_event_id(
    client: AsyncClient,
    auth_headers: dict[str, str],
    mock_runtime: MockRobotRuntime,
) -> None:
    stream = SseStream("/api/robot/events", auth_headers)
    await stream.start()
    store_id = await _run_command(
        client,
        {
            "command": "store_item",
            "item": {
                "barcode": "036000291452",
                "name": "Resume",
                "dimensions": {"width": 10, "height": 20, "length": 30},
            },
        },
    )
    first_seq, first_data = await stream.wait_for_frame(
        lambda frame: json.loads(frame[1]).get("command_id") == store_id
    )
    assert first_seq is not None
    await stream.stop()

    # The next command completes while no subscriber is connected.
    await _run_command(
        client,
        {
            "command": "retrieve_item",
            "location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        },
    )

    # Resuming from the acknowledged id replays the missed event first.
    resumed = SseStream(
        "/api/robot/events",
        auth_headers,
        extra={"Last-Event-ID": str(first_seq)},
    )
    await resumed.start()
    missed_seq, missed_data = await resumed.wait_for_frame(
        lambda frame: json.loads(frame[1]).get("command") == "retrieve_item"
    )
    assert missed_seq == first_seq + 1
    await resumed.stop()


@pytest.mark.anyio
async def test_sse_fresh_subscription_does_not_replay_history(
    client: AsyncClient,
    auth_headers: dict[str, str],
    mock_runtime: MockRobotRuntime,
) -> None:
    # Subscribe before any command: the stream must start live, and the first
    # frame is the first command's event with the log's first seq.
    stream = SseStream("/api/robot/events", auth_headers)
    await stream.start()

    await _run_command(
        client,
        {
            "command": "store_item",
            "item": {
                "barcode": "036000291452",
                "name": "History",
                "dimensions": {"width": 10, "height": 20, "length": 30},
            },
        },
    )
    first_seq, _ = await stream.wait_for_frame(
        lambda frame: json.loads(frame[1]).get("command") == "store_item"
    )
    assert first_seq == 1

    await _run_command(
        client,
        {
            "command": "retrieve_item",
            "location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        },
    )
    second_seq, _ = await stream.wait_for_frame(
        lambda frame: json.loads(frame[1]).get("command") == "retrieve_item"
    )
    assert second_seq == 2
    await stream.stop()


@pytest.mark.anyio
async def test_listener_appends_hardware_events_to_the_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QueueWorker:
        def __init__(self) -> None:
            self._subscribers: list[asyncio.Queue[str]] = []

        async def subscribe_events(self) -> asyncio.Queue[str]:
            queue: asyncio.Queue[str] = asyncio.Queue()
            self._subscribers.append(queue)
            return queue

        async def unsubscribe_events(self, queue: asyncio.Queue[str]) -> None:
            self._subscribers.remove(queue)

        async def publish_event(self, payload: dict) -> None:
            message = json.dumps(payload)
            for queue in list(self._subscribers):
                await queue.put(message)

    worker = QueueWorker()
    listener = RobotEventListener(worker)  # type: ignore[arg-type]
    await listener.start()
    monkeypatch.setattr(robot_core, "event_listener", listener)
    try:
        await worker.publish_event(
            {
                "kind": "safety_state",
                "payload": {"active": False, "motion_blocked": False},
            }
        )
        deadline = asyncio.get_running_loop().time() + 5.0
        rows: list[RobotEvent] = []
        while asyncio.get_running_loop().time() < deadline:
            rows = await _event_rows()
            if rows:
                break
            await asyncio.sleep(0.01)

        assert len(rows) == 1
        assert rows[0].payload["kind"] == "safety_state"
        assert rows[0].command_id is None
    finally:
        await listener.stop()
