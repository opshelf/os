"""Long-lived responses must not hold a pooled database connection open.

Regression for the SQLAlchemy error "The garbage collector is trying to clean up
non-checked-in connection <AdaptedConnection <Connection(Thread-N, stopped daemon
...)>>, which will be dropped, as it cannot be safely terminated".

A streamed route that authenticated through a dependency carrying `Depends(get_db)`
kept that session checked out until the body finished, because FastAPI closes a
`yield` dependency through the request exit stack only after streaming ends. The dev
UI holds the robot status/event streams and a camera view open for a whole session, so
those streams parked connections out of the five-connection pool, and the bounded
uvicorn shutdown drain (`timeout_graceful_shutdown`) abandoned them with the connection
still checked out -- which the garbage collector then finalized after the aiosqlite
worker thread was gone. A cancelled stream also could not close its session: the
cancellation interrupts `aiosqlite`'s close, so the connection never returned to the
pool at all.

Streamed routes therefore authenticate with a dependency that owns its session and
closes it before the body starts streaming.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sse_starlette.sse import EventSourceResponse as LibraryEventSourceResponse

from openshelf_os_server import main as main_module

from openshelf_os_server.core.camera import camera_registry
from openshelf_os_server.database.base import engine
from openshelf_os_server.main import app
from openshelf_os_server.middleware import audit as audit_module
from openshelf_os_server.routers import camera as camera_router
from openshelf_os_server.routers.auth_deps import get_current_user_for_stream, get_relay_or_current_user_for_stream

# Every route that answers with a body that can stay open, with the scoped auth
# dependency it uses instead of the one that carries `Depends(get_db)`.
STREAMING_ROUTES: tuple[tuple[str, Any], ...] = (
    ("/api/camera/{camera_id}", get_current_user_for_stream),
    ("/api/calibration/laser/stream", get_current_user_for_stream),
    ("/api/robot/status", get_relay_or_current_user_for_stream),
    ("/api/robot/events", get_relay_or_current_user_for_stream),
)
# Dependencies that would hold a session for the lifetime of the response.


def _checkedout() -> int:
    """Connections the engine currently holds checked out."""
    return int(engine.sync_engine.pool.checkedout())


def _dependency_calls(dependant: Any) -> set[Callable[..., Any] | None]:
    calls: set[Callable[..., Any] | None] = {getattr(dependant, "call", None)}
    for child in getattr(dependant, "dependencies", []) or []:
        calls |= _dependency_calls(child)
    return calls


def _stream_scope(path: str, headers: dict[str, str]) -> dict[str, Any]:
    """Minimal ASGI scope so a stream can be driven and cancelled directly."""
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
    }


async def _wait_for_body(sent: list[dict[str, Any]], timeout: float = 5.0) -> None:
    """Wait until the response body of a driven stream started arriving."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not any(message.get("type") == "http.response.body" for message in sent):
        if loop.time() > deadline:
            raise AssertionError("stream did not start sending a body")
        await asyncio.sleep(0.01)


def _tool_manager() -> MagicMock:
    manager = MagicMock()
    manager.first_frame_timeout = 1.0
    manager.wait_for_fresh_frame = AsyncMock(return_value=True)
    return manager


async def _noop(camera_id: str) -> None:
    return None


@pytest.mark.anyio
async def test_cancelled_sse_stream_returns_its_pooled_connection(
    seeded_db: AsyncGenerator[None, None], auth_headers: dict[str, str]
) -> None:
    """The reported failure: a stream cancelled mid-body must not leak its connection.

    A client disconnect, sse-starlette's `cancel_on_finish`, and the uvicorn shutdown
    drain all cancel the request task under a live stream.
    """
    baseline = _checkedout()
    sent: list[dict[str, Any]] = []
    disconnected = asyncio.Event()

    async def receive() -> dict[str, Any]:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    task = asyncio.create_task(app(_stream_scope("/api/robot/status", auth_headers), receive, send))
    try:
        await _wait_for_body(sent)
        during = _checkedout()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        disconnected.set()
        if task.done() is False:
            task.cancel()

    assert during == baseline
    assert _checkedout() == baseline


@pytest.mark.anyio
async def test_mjpeg_stream_still_rejects_an_unauthenticated_client(
    unauthenticated_client: AsyncClient,
) -> None:
    camera_registry["tool"] = _tool_manager()
    try:
        with (
            patch.object(camera_router, "start_camera_stream_by_id", new=_noop),
            patch.object(camera_router, "stop_camera_stream_by_id", new=_noop),
        ):
            response = await unauthenticated_client.get("/api/camera/tool")
    finally:
        camera_registry.pop("tool", None)

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


@pytest.mark.anyio
async def test_stalled_sse_client_closes_the_stream_without_an_error_report(
    seeded_db: AsyncGenerator[None, None], auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sentry OPENSHELF-API-CH/CG: a client that stops reading (sleeping laptop, dead
    Wi-Fi) trips sse-starlette's per-send timeout. SendTimeoutError is a TimeoutError,
    so the mapped 504 handler caught it after the stream had started, raised
    "response already started", and the request was reported as an unhandled error.
    """
    library_init = LibraryEventSourceResponse.__init__

    def fast_timeout_init(self: Any, *args: Any, **kwargs: Any) -> None:
        library_init(self, *args, **{**kwargs, "ping": 0.01, "send_timeout": 0.05})

    monkeypatch.setattr(LibraryEventSourceResponse, "__init__", fast_timeout_init)
    reported = MagicMock()
    monkeypatch.setattr(main_module, "capture_unhandled_exception", reported)
    monkeypatch.setattr(audit_module, "capture_unhandled_exception", reported)

    never = asyncio.Event()
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await never.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            # BaseHTTPMiddleware forwards each chunk on its own task, so the stream only
            # times out once a later ping backs up behind this stall. Finite so the
            # middleware chain can unwind.
            await asyncio.sleep(0.5)

    await asyncio.wait_for(app(_stream_scope("/api/robot/status", auth_headers), receive, send), timeout=5)

    assert sent[0] == {"type": "http.response.start", "status": 200, "headers": sent[0]["headers"]}
    reported.assert_not_called()
