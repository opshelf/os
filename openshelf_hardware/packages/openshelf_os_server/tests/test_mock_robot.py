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
async def test_mock_scan_injection_reaches_the_event_stream(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
) -> None:
	queue = await mock_runtime.subscribe_events()
	try:
		response = await client.post("/api/robot/debug/scanner/scan", json={"barcode": "ACE-500"})
		assert response.status_code == 200
		event = json.loads(await asyncio.wait_for(queue.get(), 1))
		assert event == {"kind": "label_scanned", "payload": "ACE-500"}
	finally:
		await mock_runtime.unsubscribe_events(queue)


@pytest.mark.anyio
async def test_mock_io_box_simulation_is_cleared_when_a_store_starts(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	placed = await client.post("/api/robot/debug/io-box", json={"occupied": True})
	assert placed.status_code == 200
	status = json.loads(await mock_runtime.status_payload())
	assert status["robot_status"]["scale"]["occupied"] is True

	stored = await client.post(
		"/api/robot/commands",
		json={
			"command": "store_item",
			"item": {
				"barcode": "880000000777",
				"name": "Picked from IO box",
				"dimensions": {"width": 10, "height": 10, "length": 10},
			},
		},
	)
	assert stored.status_code == 200, stored.text
	await _wait_for_status(client, stored.json()["command_id"], "succeeded")
	assert mock_runtime.is_bin_io_box_occupied() is False

	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	refused = await client.post("/api/robot/debug/io-box", json={"occupied": True})
	assert refused.status_code == 409


@pytest.mark.anyio
async def test_mock_store_with_an_import_request_id_loads_the_unit(
	client: AsyncClient,
	mock_runtime: MockRobotRuntime,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	from openshelf_os_server.core import import_jobs as import_jobs_core
	from openshelf_os_server.core.import_jobs import ImportJobService, ImportRowInput
	from openshelf_os_server.database.models import ImportJob, ImportJobUnit

	monkeypatch.setattr(import_jobs_core, "get_operating_mode", lambda: OperatingMode.mock)
	async with AsyncSessionLocal() as session:
		await session.execute(delete(ImportJobUnit))
		await session.execute(delete(ImportJob))
		await session.commit()
		service = ImportJobService(session)
		job = await service.create([ImportRowInput(name="Acetone", sku="MOCK-IMPORT-1")], "mock.csv")
		unit = (await service.units(job))[0]
		unit = await service.begin_unit(job, unit.id, {"width": 50, "height": 60, "length": 70})
		job_id, request_id = job.id, unit.request_id
	events = await mock_runtime.subscribe_events()
	try:
		stored = await client.post(
			"/api/robot/commands",
			json={"command": "store_item", "request_id": request_id, "item": {"barcode": "MOCK-IMPORT-1"}},
		)
		assert stored.status_code == 200, stored.text
		await _wait_for_status(client, stored.json()["command_id"], "succeeded")
		kinds = []
		for _ in range(10):
			kinds.append(json.loads(await asyncio.wait_for(events.get(), 1)).get("kind"))
			if "import_job_updated" in kinds:
				break
		assert "import_job_updated" in kinds
	finally:
		await mock_runtime.unsubscribe_events(events)
	async with AsyncSessionLocal() as session:
		loaded = await session.scalar(select(ImportJobUnit).where(ImportJobUnit.job_id == job_id))
		assert loaded.status == "loaded"
		assert (await session.get(ImportJob, job_id)).state == "completed"
		await session.execute(delete(ImportJobUnit))
		await session.execute(delete(ImportJob))
		await session.commit()
