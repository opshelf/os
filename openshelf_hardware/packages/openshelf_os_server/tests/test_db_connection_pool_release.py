"""A cancelled request must hand its database connection back to the pool.

Regression for `QueuePool limit of size 5 overflow 10 reached, connection timed out,
timeout 30.00`, which the platform reported against `/api/auth/login/json` wrapped in an
`ExceptionGroup` from Starlette's `create_collapsing_task_group`.

`get_db` used to close its session from a `finally` block. When a request is cancelled
while that cleanup runs — a client disconnect, a browser reload with the request in
flight, or the bounded uvicorn shutdown drain (`timeout_graceful_shutdown`) cancelling
every request still running — the cancellation interrupts `session.close()` mid-checkin:
the aiosqlite worker thread dies with the connection still checked out, and SQLAlchemy
can only drop it later under the garbage collector (the
"cannot be safely terminated" warning). Those connections are never reclaimed, so 15
leaks (pool size 5 plus overflow 10) exhaust the pool for the whole process, and every
later request that needs a connection fails after the 30 second pool timeout. The login
route is simply where an already-exhausted pool surfaced.

The cleanup now hands `session.close()` to a shielded task, which the request's own
cancellation cannot reach, so the checkin completes and the pool stays usable.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, MutableMapping
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from openshelf_os_server.database import session as session_module
from openshelf_os_server.database.base import Base, engine
from openshelf_os_server.database.session import CLOSE_WAIT_LIMIT_S, get_db

# Enough concurrent requests to exhaust the pool on its own (5 + 10 overflow), so a
# leak would show up as a pool that never empties rather than as a slow test.
CONCURRENT_REQUESTS = 8
STALLED_CLOSE_LIMIT_S = 0.5


@pytest.fixture
def pool() -> Any:
    return engine.sync_engine.pool


@pytest.fixture
def route() -> FastAPI:
    """An app with one route that queries the database and then waits forever.

    Cancelling it reproduces the reported shape: the request task is suspended inside a
    database await when the cancellation arrives, so its session is open with a
    connection checked out.
    """
    app = FastAPI()

    @app.get("/slow-query")
    async def slow_query(db: AsyncSession = Depends(get_db)) -> None:
        await db.execute(text("select 1"))
        await asyncio.sleep(3600)

    return app


def _scope(path: str) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "headers": [],
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "root_path": "",
    }


async def _receive() -> dict[str, Any]:
    await asyncio.sleep(3600)
    return {"type": "http.disconnect"}


async def _noop_send(_message: MutableMapping[str, Any]) -> None:
    return None


async def _drain(app: FastAPI, path: str, count: int, pool: Any) -> list[asyncio.Task[None]]:
    """Start `count` requests and return their tasks once each holds a connection.

    `Base.metadata.create_all` runs before this, so no DDL is in flight and every
    connection the pool holds belongs to one of the requests.
    """
    baseline = int(pool.checkedout())
    tasks = [
        asyncio.create_task(app(_scope(path), _receive, _noop_send))
        for _ in range(count)
    ]
    deadline = asyncio.get_running_loop().time() + 10
    while int(pool.checkedout()) < baseline + count:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("requests did not check out a connection each")
        await asyncio.sleep(0.01)
    return tasks


async def _pool_has_a_free_connection(pool: Any) -> bool:
    """True when a new connection can be checked out without waiting for the timeout."""
    async with asyncio.timeout(5):
        async with engine.connect() as connection:
            await connection.execute(text("select 1"))
    return True


async def test_cancelled_requests_return_their_connections(
    route: FastAPI, pool: Any
) -> None:
    """The reported failure: cancelling requests that hold a connection must not drain the pool."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    baseline = int(pool.checkedout())
    tasks = await _drain(route, "/slow-query", CONCURRENT_REQUESTS, pool)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert int(pool.checkedout()) == baseline, (
        "cancelled requests kept their connections checked out; 15 of these exhaust the pool"
    )
    assert await _pool_has_a_free_connection(pool) is True


async def test_a_normal_request_still_returns_its_connection(pool: Any) -> None:
    """The fix must not change the happy path or the plain-error path."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    baseline = int(pool.checkedout())
    agen = get_db()
    session = await agen.__anext__()
    await session.execute(text("select 1"))
    assert int(pool.checkedout()) == baseline + 1

    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()
    assert int(pool.checkedout()) == baseline


async def test_a_failing_request_returns_its_connection(pool: Any) -> None:
    """`HTTPException` (every 401 and 429) unwinds through the same cleanup path."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    baseline = int(pool.checkedout())
    agen = get_db()
    session = await agen.__anext__()
    await session.execute(text("select 1"))
    assert int(pool.checkedout()) == baseline + 1

    with pytest.raises(RuntimeError):
        await agen.athrow(RuntimeError("request failed"))
    assert int(pool.checkedout()) == baseline


async def test_a_stalled_close_does_not_hang_the_request(
    route: FastAPI, pool: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close that never returns is left to its own task instead of wedging the request."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def stalled_close(session: AsyncSession) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(session_module, "_close_or_ignore_closed_connection", stalled_close)
    monkeypatch.setattr(session_module, "CLOSE_WAIT_LIMIT_S", STALLED_CLOSE_LIMIT_S)

    tasks = await _drain(route, "/slow-query", 2, pool)
    for task in tasks:
        task.cancel()

    # Bounded by the patched wait limit, not by the stalled close itself.
    done, _ = await asyncio.wait(tasks, timeout=STALLED_CLOSE_LIMIT_S + 3)
    assert len(done) == len(tasks)
    assert await _pool_has_a_free_connection(pool) is True


async def test_a_cancelled_session_that_never_ran_a_statement_is_released(
    seeded_db: AsyncGenerator[None, None],
) -> None:
    """A session that never opened a connection is released without error."""
    agen = get_db()
    await agen.__anext__()
    with pytest.raises(asyncio.CancelledError):
        await agen.athrow(asyncio.CancelledError())


def test_close_wait_limit_is_bounded() -> None:
    """The shielded close must not wait as long as the pool timeout it is escaping."""
    assert 0 < CLOSE_WAIT_LIMIT_S < 30.0


async def test_a_directly_awaited_close_is_lost_when_the_cleanup_is_cancelled(
    pool: Any,
) -> None:
    """The failure the fix removes: a close awaited by a cancelled request is lost.

    `_close_or_ignore_closed_connection` is the exact call the pre-fix `finally` made. When
    the task running it is cancelled, the checkin is interrupted, the aiosqlite worker
    thread dies, and the connection stays checked out with nothing left to reclaim it —
    15 of those and the pool is exhausted for the rest of the process.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    baseline = int(pool.checkedout())
    agents = [get_db() for _ in range(CONCURRENT_REQUESTS)]
    sessions = []
    for agen in agents:
        session = await agen.__anext__()
        await session.execute(text("select 1"))
        sessions.append(session)
    assert int(pool.checkedout()) == baseline + CONCURRENT_REQUESTS

    async def cleanup() -> None:
        for session in sessions:
            await session_module._close_or_ignore_closed_connection(session)

    task = asyncio.create_task(cleanup())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.2)
    assert int(pool.checkedout()) > baseline, (
        "a cancelled, directly awaited close was expected to lose its connections"
    )

    # The shielded close the fix uses, on the same kind of cancelled cleanup.
    async def shielded_cleanup() -> None:
        for session in sessions:
            await session_module._run_uncancellable(
                session_module._close_or_report_exhaustion(session, "test")
            )

    task = asyncio.create_task(shielded_cleanup())
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.3)
    assert int(pool.checkedout()) == baseline


async def test_a_cancelled_session_is_closed_even_when_the_caller_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shielded close runs to completion even though its caller was cancelled."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    closes = 0

    async def counting_close(session: AsyncSession) -> None:
        nonlocal closes
        closes += 1
        await asyncio.sleep(0.05)

    monkeypatch.setattr(session_module, "_close_or_ignore_closed_connection", counting_close)

    agents = [get_db() for _ in range(3)]
    for agen in agents:
        session = await agen.__anext__()
        await session.execute(text("select 1"))
    for agen in agents:
        with contextlib.suppress(asyncio.CancelledError):
            await agen.athrow(asyncio.CancelledError())
    # The shielded closes outlive the cancelled callers; wait for them.
    deadline = asyncio.get_running_loop().time() + 5
    while closes < len(agents) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)

    assert closes == len(agents)


async def test_a_close_that_finds_the_pool_exhausted_reports_the_pool_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The reported error is logged with the pool counts, not just as a bare timeout.

    Sentry shows the login failure as a cancellation chained through the ASGI task
    group, which names neither the pool nor how many connections are held, so these
    counts are what tells a leak from load.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def exhausted_close(session: AsyncSession) -> None:
        raise SQLAlchemyTimeoutError("connect", None, Exception(
            "QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00"
        ))

    monkeypatch.setattr(session_module, "_close_or_ignore_closed_connection", exhausted_close)
    caplog.set_level(logging.ERROR, logger=session_module.logger.name)
    caplog.clear()

    agen = get_db()
    await agen.__anext__()
    with pytest.raises(SQLAlchemyTimeoutError):
        await agen.__anext__()

    assert [r.message for r in caplog.records] == ["database_connection_pool_exhausted"]
    event = caplog.records[0]
    assert event.context == "closed session"  # type: ignore[attr-defined]
    assert "Connections in pool" in event.pool_status  # type: ignore[attr-defined]


async def test_an_ordinary_close_failure_is_not_reported_as_exhaustion(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A statement or lock timeout must not be dressed up as a pool problem."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def lock_timeout(session: AsyncSession) -> None:
        raise SQLAlchemyTimeoutError("execute", None, Exception("database is locked"))

    monkeypatch.setattr(session_module, "_close_or_ignore_closed_connection", lock_timeout)
    caplog.set_level(logging.ERROR, logger=session_module.logger.name)
    caplog.clear()

    agen = get_db()
    await agen.__anext__()
    with pytest.raises(SQLAlchemyTimeoutError):
        await agen.__anext__()

    assert [r.message for r in caplog.records] == []
