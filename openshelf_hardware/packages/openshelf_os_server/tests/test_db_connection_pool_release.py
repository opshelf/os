"""A cancelled request must hand its database connection back to the pool.

Regression for `QueuePool limit of size 5 overflow 10 reached, connection timed out,
timeout 30.00`. Under BaseHTTPMiddleware anyio re-delivers a request's cancellation on
every await, so a `session.close()` awaited in the request task was interrupted and the
connection stayed checked out until the garbage collector finalized it.
"""
from __future__ import annotations

import asyncio
import gc
from typing import Any

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from openshelf_os_server.database.base import engine
from openshelf_os_server.database.session import get_db

CONCURRENT_REQUESTS = 4


async def test_cancelled_requests_return_their_connections() -> None:
    app = FastAPI()
    holding = 0

    @app.get("/slow-query")
    async def slow_query(db: AsyncSession = Depends(get_db)) -> None:
        nonlocal holding
        await db.execute(text("select 1"))
        holding += 1
        await asyncio.sleep(3600)

    async def passthrough(request: Any, call_next: Any) -> Any:
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)

    pool = engine.sync_engine.pool
    loop = asyncio.get_running_loop()
    gc.disable()
    try:
        baseline = int(pool.checkedout())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            tasks = [
                asyncio.create_task(client.get("/slow-query"))
                for _ in range(CONCURRENT_REQUESTS)
            ]
            deadline = loop.time() + 10
            while holding < CONCURRENT_REQUESTS:
                assert loop.time() < deadline, "requests did not each open a transaction"
                await asyncio.sleep(0.01)

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        deadline = loop.time() + 5
        while int(pool.checkedout()) > baseline and loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert int(pool.checkedout()) == baseline, (
            "cancelled requests kept their connections checked out until garbage collection"
        )
    finally:
        gc.enable()
        gc.collect()
