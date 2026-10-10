"""Storage API writes must reach other open clients as `inventory_changed` events."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from openshelf_os_server.core import robot as robot_core
from openshelf_os_server.database.models import Item
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.middleware import inventory_events

_BARCODE = "inventory-event-barcode"


async def _delete_item() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(delete(Item).where(Item.barcode == _BARCODE))
		await session.commit()


@pytest.mark.anyio
async def test_only_successful_storage_writes_publish(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
	publish = AsyncMock()
	monkeypatch.setattr(inventory_events, "publish_inventory_changed", publish)
	await _delete_item()
	try:
		created = await client.post("/api/storage/items", json={
			"barcode": _BARCODE,
			"name": "Event item",
			"dimensions": {"width": 10, "height": 10, "length": 10},
		})
		assert created.status_code == 200, created.text
		assert publish.await_count == 1

		lookup = await client.post("/api/storage/items/lookup", json={"barcodes": [_BARCODE]})
		assert lookup.status_code < 500
		read = await client.get("/api/storage/items")
		assert read.status_code == 200
		invalid = await client.post("/api/storage/items", json={})
		assert invalid.status_code == 422
		assert publish.await_count == 1
	finally:
		await _delete_item()


@pytest.mark.anyio
async def test_publish_reaches_hardware_event_subscribers(monkeypatch: pytest.MonkeyPatch) -> None:
	queue: asyncio.Queue[str] = asyncio.Queue()

	class Worker:
		async def publish_event(self, payload: dict) -> None:
			await queue.put(json.dumps(payload))

	monkeypatch.setattr(inventory_events, "get_operating_mode", lambda: "production")
	monkeypatch.setattr(robot_core, "robot_worker", Worker())

	await inventory_events.publish_inventory_changed()

	assert json.loads(queue.get_nowait()) == {"kind": "inventory_changed"}


@pytest.mark.anyio
async def test_publish_without_a_worker_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(inventory_events, "get_operating_mode", lambda: "production")
	monkeypatch.setattr(robot_core, "robot_worker", None)

	await inventory_events.publish_inventory_changed()
