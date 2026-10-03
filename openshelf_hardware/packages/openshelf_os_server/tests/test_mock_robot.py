from __future__ import annotations

import asyncio
import json

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.storage import Shelf
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
		async with AsyncSessionLocal() as session:
			await session.execute(delete(MockRobotCommand).where(MockRobotCommand.runtime == "mock"))
			await session.commit()


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
async def test_mock_status_storage_matches_the_shelf_wire_model(mock_runtime: MockRobotRuntime) -> None:
	storage = json.loads(await mock_runtime.status_payload())["robot_status"]["storage"]

	for side in ("front_module", "back_module"):
		for shelf in storage[side]:
			Shelf.model_validate(shelf)
			for cell in shelf["cells"]:
				assert "calibration_point" in cell


@pytest.mark.anyio
async def test_mock_status_keeps_command_id_through_queued_to_running(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	await mock_runtime.stop()
	response = await client.post(
		"/api/robot/commands",
		json={
			"command": "store_item",
			"item": {
				"barcode": "036000291452",
				"name": "Mock item",
				"dimensions": {"width": 10, "height": 20, "length": 30},
			},
		},
	)
	assert response.status_code == 200
	command_id = response.json()["command_id"]

	queued_status = json.loads(await mock_runtime.status_payload())
	assert len(queued_status["queued_operations"]) == 1
	queued_operation = queued_status["queued_operations"][0]
	assert queued_operation["command_id"] == command_id
	assert queued_operation["command"] == {
		"command": "store_item",
		"item": {
			"barcode": "036000291452",
			"name": "Mock item",
			"dimensions": {"width": 10, "height": 20, "length": 30},
		},
	}

	await mock_runtime._claim_next()
	running_status = json.loads(await mock_runtime.status_payload())
	assert running_status["current_command"] == {
		"command": "store_item",
		"item": {
			"barcode": "036000291452",
			"name": "Mock item",
			"dimensions": {"width": 10, "height": 20, "length": 30},
		},
		"command_id": command_id,
	}


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
			"barcode": "036000291452",
			"name": "Mock item",
			"dimensions": {"width": 10, "height": 20, "length": 30},
		},
	}
	response = await client.post("/api/robot/commands", json=store_payload)
	assert response.status_code == 200
	command_id = response.json()["command_id"]
	completed = await _wait_for_status(client, command_id, "succeeded")
	assert completed["result"]["location"] == {
		"side": "front_module",
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
			"location": {"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
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
		item = (await session.execute(select(Item).where(Item.barcode == "036000291452"))).scalar_one()
		assert item.weight == completed["result"]["weight"]


@pytest.mark.anyio
async def test_mock_duplicate_storage_and_invalid_location_are_realistic_4xx(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	payload = {
		"command": "store_item",
		"item": {
			"barcode": "12345678901",
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
			"location": {"side": "front_module", "shelf_idx": 4, "cell_idx": 0},
		},
	)
	assert invalid.status_code == 400


@pytest.mark.anyio
async def test_mock_store_honors_requested_location(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	requested = {"side": "back_module", "shelf_idx": 2, "cell_idx": 7}
	response = await client.post(
		"/api/robot/commands",
		json={
			"command": "store_item",
			"request_id": "requested-cell",
			"location": requested,
			"item": {
				"barcode": "036000291453",
				"name": "Placed item",
				"dimensions": {"width": 10, "height": 20, "length": 30},
			},
		},
	)
	assert response.status_code == 200
	completed = await _wait_for_status(client, response.json()["command_id"], "succeeded")
	assert completed["result"]["location"] == requested

	async with AsyncSessionLocal() as session:
		locations = (await session.execute(select(ItemLocation))).scalars().all()
		assert [(row.side, row.shelf_idx, row.cell_idx) for row in locations] == [
			("back_module", 2, 7)
		]


@pytest.mark.anyio
async def test_mock_store_rejects_unusable_requested_location(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	async def store(
		barcode: str,
		*,
		location: dict | None = None,
		dimensions: dict | None = None,
		nested: bool = False,
	):
		item = {
			"barcode": barcode,
			"name": "Item",
			"dimensions": dimensions or {"width": 1, "height": 1, "length": 1},
		}
		payload: dict = {"command": "store_item", "item": item}
		if location is not None:
			if nested:
				item["location"] = location
			else:
				payload["location"] = location
		return await client.post("/api/robot/commands", json=payload)

	first = await store(
		"036000291454", location={"side": "front_module", "shelf_idx": 0, "cell_idx": 0}
	)
	await _wait_for_status(client, first.json()["command_id"], "succeeded")

	occupied = await store(
		"036000291455", location={"side": "front_module", "shelf_idx": 0, "cell_idx": 0}
	)
	assert occupied.status_code == 409
	assert occupied.json()["detail"]["code"] == "cell_unavailable"

	too_small = await store(
		"036000291456",
		location={"side": "front_module", "shelf_idx": 1, "cell_idx": 0},
		dimensions={"width": 1000, "height": 1000, "length": 1000},
	)
	assert too_small.status_code == 409
	assert too_small.json()["detail"]["code"] == "cell_too_small"

	missing = await store(
		"036000291457", location={"side": "front_module", "shelf_idx": 9, "cell_idx": 0}
	)
	assert missing.status_code == 422
	assert missing.json()["detail"]["code"] == "cell_not_found"

	malformed = await store(
		"036000291458",
		location={"side": "front_module", "shelf_idx": "not-a-number", "cell_idx": 0},
		nested=True,
	)
	assert malformed.status_code == 422
	assert malformed.json()["detail"]["code"] == "invalid_location"

	malformed_top_level = await store(
		"036000291459",
		location={"side": "front_module", "shelf_idx": "not-a-number", "cell_idx": 0},
	)
	assert malformed_top_level.status_code == 422
	assert malformed_top_level.json()["detail"]["code"] == "invalid_location"

	for barcode, shelf_idx, nested in (
		("036000291460", "0", False),
		("036000291461", False, False),
		("036000291462", "0", True),
	):
		wrong_type = await store(
			barcode,
			location={"side": "front_module", "shelf_idx": shelf_idx, "cell_idx": 0},
			nested=nested,
		)
		assert wrong_type.status_code == 422
		assert wrong_type.json()["detail"] == {
			"message": "location requires side, shelf_idx and cell_idx",
			"code": "invalid_location",
		}


@pytest.mark.anyio
async def test_mock_store_accepts_long_barcode_but_rejects_blank_or_non_string(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	long_barcode = "b" * 100
	accepted = await client.post(
		"/api/robot/commands",
		json={
			"command": "store_item",
			"item": {
				"barcode": long_barcode,
				"name": "Long barcode item",
				"dimensions": {"width": 10, "height": 20, "length": 30},
			},
		},
	)
	assert accepted.status_code == 200, accepted.text
	await _wait_for_status(client, accepted.json()["command_id"], "succeeded")

	for barcode in ("", "   ", 1234567890123456789):
		rejected = await client.post(
			"/api/robot/commands",
			json={
				"command": "store_item",
				"item": {
				"barcode": barcode,
				"name": "Item",
				"dimensions": {"width": 1, "height": 1, "length": 1},
			},
			},
		)
		assert rejected.status_code == 422
		assert rejected.json()["detail"]["message"] == "item.barcode is required"


@pytest.mark.anyio
async def test_mock_status_has_eighty_cells_and_db_occupancy(
	mock_runtime: MockRobotRuntime,
) -> None:
	async with AsyncSessionLocal() as session:
		item = Item(
			barcode="occupancy-1",
			name="Occupied",
			dimensions={"width": 1, "height": 1, "length": 1},
		)
		session.add(item)
		await session.flush()
		session.add(ItemLocation(item_id=item.id, side="front_module", shelf_idx=1, cell_idx=2))
		await session.commit()

	try:
		payload = await mock_runtime.status_payload()
	finally:
		async with AsyncSessionLocal() as session:
			await session.execute(delete(ItemLocation))
			await session.execute(delete(Item))
			await session.commit()

	status = json.loads(payload)
	assert status["mode"] == "mock"
	assert status["ready"] is True
	assert sum(
		len(shelf["cells"])
		for side in ("front_module", "back_module")
		for shelf in status["robot_status"]["storage"][side]
	) == 80
	assert status["robot_status"]["gantry"]["x_axis"]["homed"] == 1
	assert status["robot_status"]["scale"]["occupied"] is False
	assert status["robot_status"]["cabinet_doors"]["closed"] is True

	cells = status["robot_status"]["storage"]["front_module"][1]["cells"]
	assert cells[2]["status"] == "occupied"
	assert cells[3]["status"] == "available"


@pytest.mark.anyio
async def test_mock_start_marks_interrupted_commands_failed() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(delete(MockRobotCommand))
		row = MockRobotCommand(
			command_id="00000000-0000-0000-0000-000000000001",
			request_id="interrupted",
			payload={"command": "store_item", "item": {"barcode": "1"}},
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
					payload={"command": "store_item", "item": {"barcode": str(index)}},
					status="queued",
				)
			)
		await session.commit()

	response = await client.post(
		"/api/robot/commands",
		json={
			"command": "store_item",
			"item": {
				"barcode": "queue-overflow",
				"name": "Overflow",
				"dimensions": {"width": 1, "height": 1, "length": 1},
			},
		},
	)
	assert response.status_code == 429
	assert response.headers["Retry-After"] == "1"
	async with AsyncSessionLocal() as session:
		assert (await session.execute(select(Item).where(Item.barcode == "queue-overflow"))).scalar_one_or_none() is None


@pytest.mark.anyio
@pytest.mark.parametrize("dimension", ["width", "height", "length"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), "NaN", "Infinity", True, 0, -1, "10"])
async def test_mock_invalid_enrollment_dimensions_allow_corrected_retry(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
	dimension: str,
	invalid: object,
) -> None:
	payload = {
		"command": "store_item",
		"request_id": "corrected-enrollment",
		"item": {
			"barcode": "invalid-dimensions",
			"name": "Bottle",
			"dimensions": {"width": 10, "height": 10, "length": 10, dimension: invalid},
			"tare_weight": 100,
			"net_weight": 250.5,
			"full_net_weight": 500,
		},
	}
	response = await client.post(
		"/api/robot/commands",
		content=json.dumps(payload),
		headers={"Content-Type": "application/json"},
	)
	assert response.status_code == 422, response.text
	async with AsyncSessionLocal() as session:
		assert (await session.execute(select(Item))).scalars().all() == []
		assert (await session.execute(select(MockRobotCommand))).scalars().all() == []

	payload["item"]["dimensions"][dimension] = 10
	response = await client.post("/api/robot/commands", json=payload)
	assert response.status_code == 200, response.text
	await _wait_for_status(client, response.json()["command_id"], "succeeded")
	async with AsyncSessionLocal() as session:
		item = (await session.execute(select(Item).where(Item.barcode == "invalid-dimensions"))).scalar_one()
		assert item.dimensions == {"width": 10, "height": 10, "length": 10}
		assert item.weight == 350.5 and item.tare_weight == 100 and item.full_net_weight == 500


@pytest.mark.anyio
@pytest.mark.parametrize("interruption", ["oversized", "restart"])
async def test_mock_enrollment_survives_failure_and_editable_retry(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
	interruption: str,
) -> None:
	if interruption == "restart":
		await mock_runtime.stop()
	payload = {
		"command": "store_item",
		"request_id": "durable-enrollment",
		"item": {
			"barcode": "durable-mass",
			"name": "Bottle",
			"dimensions": {"width": 301, "height": 10, "length": 10},
			"metadata": {"label": "Original"},
			"tare_weight": 100,
			"net_weight": 250.5,
			"full_net_weight": 500,
		},
	}
	response = await client.post("/api/robot/commands", json=payload)
	assert response.status_code == 200, response.text
	command_id = response.json()["command_id"]
	if interruption == "restart":
		assert (await mock_runtime.get_command(command_id)).status == "queued"
		# A fresh runtime reads the durable enrollment and interrupted command.
		restarted = MockRobotRuntime(execution_seconds=0.01)
		await restarted.start()
		await restarted.stop()
		await mock_runtime.start()
	failed = await _wait_for_status(client, command_id, "failed")
	if interruption == "restart":
		assert failed["result"]["code"] == "service_restarted"
	else:
		assert failed["result"]["error"] == "No suitable storage location found"
	async with AsyncSessionLocal() as session:
		item = (await session.execute(select(Item).where(Item.barcode == "durable-mass"))).scalar_one()
		assert item.weight == 350.5
		assert item.tare_weight == 100 and item.full_net_weight == 500
		assert item.enrollment_weight_pending and not item.enrollment_storage_completed
		assert item.name == "Bottle" and item.item_metadata == {"label": "Original"}
		assert (await session.execute(select(ItemLocation).where(ItemLocation.item_id == item.id))).first() is None

	edited = await client.post("/api/storage/items/update", json={
		"barcode": "durable-mass",
		"dimensions": {"width": 10, "height": 10, "length": 10},
		"net_weight": 260.5,
	})
	assert edited.status_code == 200, edited.text
	# Replaying the old request must not restore its original mass or dimensions.
	accepted = await mock_runtime.get_command(command_id)
	replayed, is_replay = await mock_runtime.enqueue(
		accepted.payload,
		request_id=payload["request_id"], correlation_id=None,
	)
	assert is_replay and replayed.command_id == command_id
	response = await client.post("/api/robot/commands", json={
		"command": "store_item", "item": {"barcode": "durable-mass"},
	})
	assert response.status_code == 200, response.text
	succeeded = await _wait_for_status(client, response.json()["command_id"], "succeeded")
	assert succeeded["result"]["weight"] == 350
	async with AsyncSessionLocal() as session:
		item = (await session.execute(select(Item).where(Item.barcode == "durable-mass"))).scalar_one()
		assert item.weight == 360.5 and item.net_weight == 260.5
		assert item.tare_weight == 100 and item.full_net_weight == 500
		assert not item.enrollment_weight_pending and item.enrollment_storage_completed


@pytest.mark.anyio
async def test_mock_manual_mass_cycle_and_scale(client: AsyncClient, mock_runtime: MockRobotRuntime) -> None:
	reading = await client.get('/api/robot/debug/scale')
	assert reading.status_code == 200
	assert reading.json()['weight_grams'] == 350
	assert reading.json()['status']['stable'] and reading.json()['status']['valid']
	response = await client.post('/api/robot/commands', json={
		'command': 'store_item', 'request_id': 'mass-mock-first', 'item': {
			'barcode': 'mass-mock', 'name': 'Bottle', 'dimensions': {'width': 10, 'height': 10, 'length': 10},
			'tare_weight': 100, 'net_weight': 250.5, 'full_net_weight': 500,
		}})
	assert response.status_code == 200, response.text
	result = await _wait_for_status(client, response.json()['command_id'], 'succeeded')
	assert result['result']['weight'] == 350
	item = (await client.post("/api/storage/items/lookup", json={"barcode": "mass-mock"})).json()
	assert item['weight'] == 350.5 and item['net_weight'] == 250.5
	response = await client.post('/api/robot/commands', json={
		'command': 'retrieve_item', 'location': item['locations'][0], 'request_id': 'mass-mock-second'})
	assert response.status_code == 200, response.text
	await _wait_for_status(client, response.json()['command_id'], 'succeeded')
	item = (await client.post("/api/storage/items/lookup", json={"barcode": "mass-mock"})).json()
	assert item['weight'] == 350 and item['net_weight'] == 250
