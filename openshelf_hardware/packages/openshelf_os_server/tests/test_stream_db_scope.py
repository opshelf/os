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
from fastapi import HTTPException
from httpx import AsyncClient
from starlette.requests import Request

from openshelf_os_server.core.camera import camera_registry
from openshelf_os_server.database.base import engine
from openshelf_os_server.database.models import User
from openshelf_os_server.database.session import get_db
from openshelf_os_server.main import app
from openshelf_os_server.routers import camera as camera_router
from openshelf_os_server.routers.auth_deps import (
    get_current_user,
    get_current_user_for_stream,
    get_relay_or_current_user,
    get_relay_or_current_user_for_stream,
)

# Every route that answers with a body that can stay open, with the scoped auth
# dependency it uses instead of the one that carries `Depends(get_db)`.
STREAMING_ROUTES: tuple[tuple[str, Any], ...] = (
    ("/api/camera/{camera_id}", get_current_user_for_stream),
    ("/api/calibration/laser/stream", get_current_user_for_stream),
    ("/api/robot/status", get_relay_or_current_user_for_stream),
    ("/api/robot/events", get_relay_or_current_user_for_stream),
)
# Dependencies that would hold a session for the lifetime of the response.
SESSION_HELD_CALLS = (get_db, get_current_user, get_relay_or_current_user)


def _checkedout() -> int:
    """Connections the engine currently holds checked out."""
    return int(engine.sync_engine.pool.checkedout())


def _dependency_calls(dependant: Any) -> set[Callable[..., Any] | None]:
    calls: set[Callable[..., Any] | None] = {getattr(dependant, "call", None)}
    for child in getattr(dependant, "dependencies", []) or []:
        calls |= _dependency_calls(child)
    return calls


def _route_calls(path: str) -> set[Callable[..., Any] | None]:
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if getattr(route, "path", None) == path and "GET" in methods:
            return _dependency_calls(route.dependant)
    raise AssertionError(f"GET route not found: {path}")


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
async def test_mjpeg_stream_holds_no_pooled_connection(client: AsyncClient) -> None:
    baseline = _checkedout()
    checkouts: list[int] = []

    async def fake_frames() -> AsyncGenerator[bytes, None]:
        for _ in range(3):
            # Sampled while the body is streaming: the auth session has to be gone
            # by now, otherwise this stream parks a pooled connection.
            checkouts.append(_checkedout())
            await asyncio.sleep(0.02)
            yield b"frame"

    camera_registry["tool"] = _tool_manager()
    try:
        with (
            patch.object(camera_router, "start_camera_stream_by_id", new=_noop),
            patch.object(camera_router, "stop_camera_stream_by_id", new=_noop),
            patch.object(
                camera_router,
                "mjpeg_stream_generator_for_camera",
                new=lambda manager, request: fake_frames(),
            ),
        ):
            response = await client.get("/api/camera/tool")
    finally:
        camera_registry.pop("tool", None)

    assert response.status_code == 200
    assert len(checkouts) == 3
    assert max(checkouts) == baseline
    assert _checkedout() == baseline


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


@pytest.mark.parametrize("path,scoped_dependency", STREAMING_ROUTES)
def test_streaming_routes_do_not_authenticate_with_a_pinned_session(
    path: str, scoped_dependency: Callable[..., Any]
) -> None:
    calls = _route_calls(path)

    assert scoped_dependency in calls
    # No dependency in the graph may carry a session, otherwise the stream keeps that
    # connection checked out until the body ends.
    assert calls.isdisjoint(SESSION_HELD_CALLS)


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
async def test_stream_auth_dependency_closes_its_session(auth_headers: dict[str, str]) -> None:
    baseline = _checkedout()
    token = auth_headers["Authorization"].split(" ")[1]

    user = await get_current_user_for_stream(token=token)

    assert isinstance(user, User)
    assert user.email == "admin@opshelf.com"
    # The session is closed inside the dependency, so whatever a stream route reads
    # off the user has to be loaded already.
    assert user.role is not None
    assert "robot:control" in (user.role.permissions or [])
    assert _checkedout() == baseline


@pytest.mark.anyio
async def test_stream_auth_dependency_rejects_an_invalid_token() -> None:
    with pytest.raises(HTTPException) as excinfo:
        await get_current_user_for_stream(token="not-a-token")

    assert excinfo.value.status_code == 401
    assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.anyio
async def test_relay_stream_dependency_closes_its_session(auth_headers: dict[str, str]) -> None:
    baseline = _checkedout()
    token = auth_headers["Authorization"].split(" ")[1]
    request = Request(_stream_scope("/api/robot/status", auth_headers))

    user = await get_relay_or_current_user_for_stream(request=request, token=token)

    assert isinstance(user, User)
    assert user.email == "admin@opshelf.com"
    assert request.state.relay_service_authenticated is False
    assert user.role is not None
    assert _checkedout() == baseline


@pytest.mark.anyio
async def test_relay_stream_dependency_rejects_an_invalid_token() -> None:
    request = Request(_stream_scope("/api/robot/status", {}))

    with pytest.raises(HTTPException) as excinfo:
        await get_relay_or_current_user_for_stream(request=request, token="not-a-token")

    assert excinfo.value.status_code == 401
