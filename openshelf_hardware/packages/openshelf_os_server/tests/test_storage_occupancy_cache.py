"""Tests for the commit-scoped storage occupancy cache used by the status stream."""

from __future__ import annotations

import asyncio
import json
from typing import Any, List

import pytest
from sqlalchemy import select

from openshelf_os_server.core import occupancy_cache
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router


def _status_payload(status: str = "available") -> str:
    return json.dumps(
        {
            "robot_status": {
                "storage": {
                    "front_module": [
                        {
                            "shelf_idx": 0,
                            "cells": [{"cell_idx": 1, "status": status}],
                        }
                    ]
                }
            }
        }
    )


def _fake_occupied_reader(readings: List[List[tuple]]) -> tuple[List[int], Any]:
    calls: List[int] = []

    async def fake(self) -> list[tuple]:
        calls.append(len(calls))
        return list(readings[min(len(calls) - 1, len(readings) - 1)])

    return calls, fake


async def _commit() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(select(1))
        await session.commit()


@pytest.mark.anyio
async def test_snapshot_reuses_one_read_until_a_commit(monkeypatch) -> None:
    occupancy_cache.reset()
    calls, fake = _fake_occupied_reader([[("front_module", 0, 1, "880000000001")]])
    monkeypatch.setattr(StorageService, "get_all_occupied_cells", fake)

    first = await occupancy_cache.get_occupied_cells()
    second = await occupancy_cache.get_occupied_cells()

    assert first == {("front_module", 0, 1)}
    assert second == {("front_module", 0, 1)}
    assert len(calls) == 1

    await _commit()

    third = await occupancy_cache.get_occupied_cells()

    assert third == {("front_module", 0, 1)}
    assert len(calls) == 2


@pytest.mark.anyio
async def test_snapshot_reflects_a_real_inventory_commit() -> None:
    """A committed item location must reach the next snapshot without a restart."""
    occupancy_cache.reset()

    assert await occupancy_cache.get_occupied_cells() == set()

    async with AsyncSessionLocal() as session:
        service = StorageService(session)
        await service.create_item(
            barcode="occupancy-item",
            name="Occupancy Item",
            dimensions={"width": 10, "height": 10, "length": 10},
        )
        await service.add_item_location(
            barcode="occupancy-item", side="front_module", shelf_idx=0, cell_idx=1
        )

    assert await occupancy_cache.get_occupied_cells() == {("front_module", 0, 1)}


class _FakeWorker:
    def __init__(self) -> None:
        self.status_queue: asyncio.Queue[str] = asyncio.Queue()
        self.unsubscribed = False

    async def subscribe_status(self) -> tuple[asyncio.Queue[str], str]:
        return self.status_queue, _status_payload()

    async def get_status_snapshot(self, force_refresh: bool = False) -> str:
        return _status_payload("occupied")

    async def unsubscribe_status(self, queue: asyncio.Queue[str]) -> None:
        self.unsubscribed = True


@pytest.mark.anyio
async def test_status_stream_reads_occupancy_once_per_commit(monkeypatch) -> None:
    """Status frames must not re-read occupancy rows that no commit replaced."""
    occupancy_cache.reset()
    calls, fake = _fake_occupied_reader([[("front_module", 0, 1, "880000000001")]])
    monkeypatch.setattr(StorageService, "get_all_occupied_cells", fake)

    worker = _FakeWorker()

    async def get_worker() -> _FakeWorker:
        return worker

    monkeypatch.setattr(robot_router, "get_robot_worker", get_worker)

    response = await robot_router.stream_sse()
    frames = response.body_iterator

    try:
        await anext(frames)  # init_progress
        await anext(frames)  # first status snapshot: one DB read
        assert len(calls) == 1

        for _ in range(3):
            await worker.status_queue.put(_status_payload())
            await anext(frames)

        assert len(calls) == 1

        await _commit()
        await worker.status_queue.put(_status_payload())
        await anext(frames)

        assert len(calls) == 2
    finally:
        await frames.aclose()

    assert worker.unsubscribed is True
