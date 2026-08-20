from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from openshelf_os.operating_mode import OperatingMode
from openshelf_os_server.core import mock_robot as mock_core
from openshelf_os_server.core.mock_robot import MockRobotRuntime
from openshelf_os_server.database.models import Item, ItemLocation, MockRobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router


@pytest.fixture
async def mock_runtime(monkeypatch: pytest.MonkeyPatch) -> MockRobotRuntime:
	async with AsyncSessionLocal() as session:
		await session.execute(delete(MockRobotCommand))
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


async def _wait_for_status(client: AsyncClient, command_id: str, expected: str) -> dict:
	for _ in range(500):
		response = await client.get(f"/api/robot/commands/{command_id}")
		assert response.status_code == 200
		if response.json()["status"] == expected:
			return response.json()
		await asyncio.sleep(0.01)
	raise AssertionError(f"command {command_id} did not reach {expected}")


def test_mock_runtime_defaults_to_hardware_execution_delay() -> None:
	assert MockRobotRuntime().execution_seconds == 30.0


@pytest.mark.anyio
async def test_mock_store_retrieve_persists_inventory_and_replays_request(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	event_queue = await mock_runtime.subscribe_events()
	store_payload = {
		"command": "store_item",
		"request_id": "store-once",
		"item": {
			"upca": "036000291452",
			"name": "Mock item",
			"dimensions": {"width": 10, "height": 20, "length": 30},
		},
	}
	response = await client.post("/api/robot/commands", json=store_payload)
	assert response.status_code == 200
	command_id = response.json()["command_id"]
	completed = await _wait_for_status(client, command_id, "succeeded")
	assert completed["result"]["location"] == {
		"side": "left_module",
		"shelf_idx": 0,
		"cell_idx": 0,
	}
	assert completed["result"]["weight"] > 0
	event = json.loads(await asyncio.wait_for(event_queue.get(), timeout=1))
	assert event["command_id"] == command_id
	assert event["command"] == "store_item"

	replay = await client.post("/api/robot/commands", json=store_payload)
	assert replay.status_code == 200
	assert replay.json() == {
		"message": "success",
		"command_id": command_id,
		"status": "succeeded",
	}

	conflict_payload = {**store_payload, "item": {**store_payload["item"], "name": "Changed"}}
	conflict = await client.post("/api/robot/commands", json=conflict_payload)
	assert conflict.status_code == 409

	retrieve = await client.post(
		"/api/robot/commands",
		json={
			"command": "retrieve_item",
			"request_id": "retrieve-once",
			"location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
		},
	)
	assert retrieve.status_code == 200
	retrieved = await _wait_for_status(client, retrieve.json()["command_id"], "succeeded")
	assert retrieved["result"]["weight"] == completed["result"]["weight"]
	retrieve_event = json.loads(await asyncio.wait_for(event_queue.get(), timeout=1))
	assert retrieve_event["command_id"] == retrieve.json()["command_id"]
	assert retrieve_event["weight"] == completed["result"]["weight"]

	async with AsyncSessionLocal() as session:
		assert (await session.execute(select(ItemLocation))).scalars().all() == []
		item = (await session.execute(select(Item).where(Item.upca == "036000291452"))).scalar_one()
		assert item.weight == completed["result"]["weight"]


@pytest.mark.anyio
async def test_mock_duplicate_storage_and_invalid_location_are_realistic_4xx(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	payload = {
		"command": "store_item",
		"item": {
			"upca": "12345678901",
			"name": "Item",
			"dimensions": {"width": 1, "height": 1, "length": 1},
		},
	}
	first = await client.post("/api/robot/commands", json=payload)
	await _wait_for_status(client, first.json()["command_id"], "succeeded")
	duplicate = await client.post("/api/robot/commands", json=payload)
	assert duplicate.status_code == 409

	invalid = await client.post(
		"/api/robot/commands",
		json={
			"command": "retrieve_item",
			"location": {"side": "left_module", "shelf_idx": 4, "cell_idx": 0},
		},
	)
	assert invalid.status_code == 400


@pytest.mark.anyio
async def test_mock_status_has_eighty_cells_and_db_occupancy(
	mock_runtime: MockRobotRuntime,
) -> None:
	payload = await mock_runtime.status_payload()
	status = json.loads(payload)
	assert status["mode"] == "mock"
	assert status["ready"] is True
	assert sum(
		len(shelf["cells"])
		for side in ("left_module", "right_module")
		for shelf in status["robot_status"]["storage"][side]
	) == 80
	assert status["robot_status"]["gantry"]["x_axis"]["homed"] == 1


@pytest.mark.anyio
async def test_mock_start_marks_interrupted_commands_failed() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(delete(MockRobotCommand))
		row = MockRobotCommand(
			command_id="00000000-0000-0000-0000-000000000001",
			request_id="interrupted",
			payload={"command": "store_item", "item": {"upca": "1"}},
			status="running",
		)
		session.add(row)
		await session.commit()

	runtime = MockRobotRuntime(execution_seconds=0.01)
	await runtime.start()
	try:
		row = await runtime.get_command("00000000-0000-0000-0000-000000000001")
		assert row is not None
		assert row.status == "failed"
		assert row.result is not None
		assert row.result["code"] == "service_restarted"
	finally:
		await runtime.stop()


@pytest.mark.anyio
async def test_mock_queue_limit_returns_retry_after(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	await mock_runtime.stop()
	async with AsyncSessionLocal() as session:
		for index in range(32):
			session.add(
				MockRobotCommand(
					command_id=f"00000000-0000-0000-0000-{index:012d}",
					payload={"command": "store_item", "item": {"upca": str(index)}},
					status="queued",
				)
			)
		await session.commit()

	response = await client.post(
		"/api/robot/commands",
		json={
			"command": "store_item",
			"item": {
				"upca": "queue-overflow",
				"name": "Overflow",
				"dimensions": {"width": 1, "height": 1, "length": 1},
			},
		},
	)
	assert response.status_code == 429
	assert response.headers["Retry-After"] == "1"
