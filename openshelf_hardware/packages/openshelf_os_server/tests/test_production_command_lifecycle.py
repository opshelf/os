from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
from openshelf_os.robot.models import QueuedOperation
from openshelf_os.storage import Cell, CellStatus, Dimensions, Shelf, StorageGrid, StorageModuleCellLocation, StorageModuleSide
from openshelf_os_server.core import command_lifecycle
from openshelf_os_server.core.command_lifecycle import (
	COMMAND_LIMIT,
	CommandRequestConflictError,
	admit,
	enqueue_internal,
	recover_interrupted_commands,
	handle_worker_lifecycle,
)
from openshelf_os_server.core.mock_robot import MockRequestConflictError, MockRobotRuntime
from openshelf_os_server.database.models import Item, RobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import robot as robot_router


class RecordingWorker:
	def __init__(self) -> None:
		self.enqueued: list[tuple[StoreItemCommand, str | None, str | None]] = []

	def needs_home(self) -> bool:
		return False

	async def enqueue_command(
		self,
		command: StoreItemCommand,
		*,
		command_id: str | None = None,
		visibility: str | None = None,
	) -> None:
		self.enqueued.append((command, command_id, visibility))


async def _clear_production_commands() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(delete(RobotCommand).where(RobotCommand.runtime == "production"))
		await session.commit()


def _store(request_id: str, *, name: str = "Lifecycle item") -> StoreItemCommand:
	return StoreItemCommand(
		request_id=request_id,
		item={
			"barcode": "880000000001",
			"name": name,
			"dimensions": {"width": 10, "height": 20, "length": 30},
		},
	)


def _store_grid(occupied: set[int] | None = None) -> StorageGrid:
	occupied = occupied or set()
	return StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_width=400.0,
				cells=[
					Cell(
						cell_idx=cell_idx,
						status=CellStatus.OCCUPIED if cell_idx in occupied else CellStatus.AVAILABLE,
						item=None,
						calibration_point=None,
						dimensions=Dimensions(width=100.0, length=100.0, height=100.0),
					)
					for cell_idx in (0, 1)
				],
			)
		],
		right_module=[],
	)


def _store_with_location(request_id: str, barcode: str, location: dict[str, int | str]) -> StoreItemCommand:
	request = _store(request_id)
	item = dict(request.item or {})
	item["barcode"] = barcode
	return StoreItemCommand(request_id=request_id, location=location, item=item)


@pytest.mark.anyio
async def test_demo_store_admits_occupied_requested_cell_for_mapped_barcode(
	client: AsyncClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	worker._robot = SimpleNamespace(
		_grid=_store_grid(occupied={1}),
		_demo_cell_map={
			"880000000200": StorageModuleCellLocation(
				side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0,
			),
		},
	)
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.demo)
	monkeypatch.setattr(robot_router, "get_settings", lambda: SimpleNamespace(robot=SimpleNamespace(enabled=True)))
	monkeypatch.setattr(robot_router, "get_robot_worker", AsyncMock(return_value=worker))
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", AsyncMock())
	try:
		response = await client.post("/api/robot/commands", json={
			"command": "store_item",
			"request_id": "demo-mapped-occupied-request",
			"location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 1},
			"item": {
				"barcode": "880000000200",
				"name": "Mapped item",
				"dimensions": {"width": 10, "height": 20, "length": 30},
			},
		})
		assert response.status_code == 200
		assert len(worker.enqueued) == 1
	finally:
		await _clear_production_commands()


@pytest.mark.anyio
@pytest.mark.parametrize("dimensions", [
	None,
	{},
	{"width": 100, "height": 100},
	"100x100x100",
	*[{'width': value, 'height': 100, 'length': 100}
	  for value in (None, 0, -1, float('nan'), float('inf'), float('-inf'), True, '100')],
])
async def test_new_store_requires_valid_dimensions_before_enrollment(
	monkeypatch: pytest.MonkeyPatch, dimensions: object,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	observer = AsyncMock()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", observer)
	item_data = {"barcode": "geometry-retry", "name": "Bottle", "tare_weight": 100, "net_weight": 250}
	if dimensions is not None:
		item_data["dimensions"] = dimensions
	request_id = "geometry-corrected-retry"

	async with AsyncSessionLocal() as db:
		await db.execute(delete(Item).where(Item.barcode == item_data["barcode"]))
		await db.commit()
		with pytest.raises(HTTPException) as invalid:
			await robot_router.robot_commands(
				StoreItemCommand(item=item_data, request_id=request_id),
				worker=worker, current_user=SimpleNamespace(id=1), db=db,
			)
		assert invalid.value.status_code == 422
		assert (await db.execute(select(Item).where(Item.barcode == item_data["barcode"]))).scalar_one_or_none() is None
		assert (await db.execute(select(RobotCommand).where(RobotCommand.runtime == "production"))).scalars().all() == []
		assert worker.enqueued == []
		observer.assert_not_awaited()

		geometry = {"width": 100, "height": 100, "length": 100}
		item_data["dimensions"] = geometry
		accepted = await robot_router.robot_commands(
			StoreItemCommand(item=item_data, request_id=request_id),
			worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert accepted.status == "queued"
		item = (await db.execute(select(Item).where(Item.barcode == item_data["barcode"]))).scalar_one()
		assert item.dimensions == geometry
		assert item.weight == 350 and item.enrollment_weight_pending
		assert worker.enqueued[0][0].item["dimensions"] == geometry
		row = (await db.execute(select(RobotCommand).where(RobotCommand.command_id == accepted.command_id))).scalar_one()
		assert row.payload["item"]["dimensions"] == geometry

		# A registered item's geometry stays authoritative on later attempts.
		await robot_router.robot_commands(
			StoreItemCommand(item={"barcode": item_data["barcode"], "dimensions": {}}, request_id="geometry-existing"),
			worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert worker.enqueued[1][0].item["dimensions"] == geometry
		await db.delete(item)
		await db.commit()
	await _clear_production_commands()


@pytest.mark.anyio
@pytest.mark.parametrize("weight", [0, -1])
async def test_new_store_rejects_nonpositive_gross_before_admission(
	monkeypatch: pytest.MonkeyPatch, weight: float,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	enqueue = AsyncMock(side_effect=RuntimeError("enqueue must not run"))
	monkeypatch.setattr(worker, "enqueue_command", enqueue)
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	observer = AsyncMock()
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", observer)
	item_data = {
		"barcode": "gross-invalid", "name": "Bottle",
		"dimensions": {"width": 100, "height": 100, "length": 100},
		"weight": weight, "tare_weight": 100, "full_net_weight": 500,
	}
	async with AsyncSessionLocal() as db:
		items_before = (await db.execute(select(Item.__table__))).all()
		with pytest.raises(HTTPException) as rejected:
			await robot_router.robot_commands(
				StoreItemCommand(item=item_data, request_id="gross-invalid"),
				worker=worker, current_user=SimpleNamespace(id=1), db=db,
			)
		assert rejected.value.status_code == 422
		assert rejected.value.detail == "Weight must be a positive number"
		await db.commit()
	async with AsyncSessionLocal() as db:
		assert (await db.execute(select(Item.__table__))).all() == items_before
		assert (await db.execute(select(RobotCommand).where(RobotCommand.runtime == "production"))).scalars().all() == []
	enqueue.assert_not_awaited()
	observer.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("gross", [{"weight": 350}, {"weight": None}, {}])
async def test_new_store_preserves_valid_and_unspecified_gross(
	monkeypatch: pytest.MonkeyPatch, gross: dict,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", AsyncMock())
	item_data = {
		"barcode": "gross-valid", "name": "Bottle",
		"dimensions": {"width": 100, "height": 100, "length": 100},
		"tare_weight": 100, "full_net_weight": 500, **gross,
	}
	async with AsyncSessionLocal() as db:
		accepted = await robot_router.robot_commands(
			StoreItemCommand(item=item_data, request_id="gross-valid"),
			worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert accepted.status == "queued"
		assert worker.enqueued[0][0].item == item_data
		row = (await db.execute(select(RobotCommand).where(RobotCommand.command_id == accepted.command_id))).scalar_one()
		assert row.payload["item"] == item_data
		item = (await db.execute(select(Item).where(Item.barcode == item_data["barcode"]))).scalar_one()
		assert item.weight == gross.get("weight")
		assert item.tare_weight == 100 and item.full_net_weight == 500
		# Existing measured zero remains authoritative over submitted gross.
		item.weight = 0
		await db.commit()
		await robot_router.robot_commands(
			StoreItemCommand(item={"barcode": item.barcode, "weight": -1}, request_id="gross-registered"),
			worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert worker.enqueued[1][0].item["weight"] == 0
		await db.delete(item)
		await db.commit()
	await _clear_production_commands()


@pytest.mark.anyio
@pytest.mark.parametrize("identity", [
	{}, {"barcode": None}, {"barcode": ""}, {"barcode": "  "}, {"barcode": 123},
	{"barcode": True}, {"barcode": {}}, {"barcode": []}, {"barcode": "x" * 256},
])
async def test_mass_store_requires_explicit_identity_before_mutation(
	monkeypatch: pytest.MonkeyPatch, identity: dict,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	observer = AsyncMock()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", observer)
	item_data = {
		"name": "Identity required bottle",
		"dimensions": {"width": 100, "height": 100, "length": 100},
		**identity,
		# One mass key is enough: enrollment is triggered by any of them, and the
		# full mass-with-identity persistence is owned by
		# test_mass_store_with_identity_persists_baseline_and_replays_before_validation.
		"tare_weight": 100,
	}
	async with AsyncSessionLocal() as db:
		items_before = (await db.execute(select(Item.__table__))).all()
		with pytest.raises(HTTPException) as invalid:
			await robot_router.robot_commands(
				StoreItemCommand(item=item_data, request_id="mass-identity-retry"),
				worker=worker, current_user=SimpleNamespace(id=1), db=db,
			)
		assert invalid.value.status_code == 422
		assert invalid.value.detail == "item.barcode is required for container mass enrollment"
		assert (await db.execute(select(Item.__table__))).all() == items_before
		assert (await db.execute(select(RobotCommand).where(RobotCommand.runtime == "production"))).scalars().all() == []
		assert worker.enqueued == []
		observer.assert_not_awaited()


@pytest.mark.anyio
async def test_mass_store_with_identity_persists_baseline_and_replays_before_validation(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", AsyncMock())
	command = StoreItemCommand(request_id="mass-explicit-identity", item={
		"barcode": "mass-explicit-identity", "name": "Bottle",
		"dimensions": {"width": 100, "height": 100, "length": 100},
		"tare_weight": 100, "net_weight": 250, "full_net_weight": 500,
	})
	async with AsyncSessionLocal() as db:
		accepted = await robot_router.robot_commands(
			command, worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert accepted.status == "queued"
		item = (await db.execute(select(Item).where(Item.barcode == command.item["barcode"]))).scalar_one()
		assert item.tare_weight == 100 and item.full_net_weight == 500
		assert item.weight == 350 and item.enrollment_weight_pending
		monkeypatch.setattr(robot_router, "_validate_direct_store_reservation", AsyncMock(side_effect=AssertionError))
		replayed = await robot_router.robot_commands(
			command, worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert replayed.command_id == accepted.command_id
		assert len(worker.enqueued) == 1
		with pytest.raises(HTTPException) as conflict:
			await robot_router.robot_commands(
				StoreItemCommand(request_id=command.request_id, item={**command.item, "barcode": None}),
				worker=worker, current_user=SimpleNamespace(id=1), db=db,
			)
		assert conflict.value.status_code == 409
		await db.delete(item)
		await db.commit()
	await _clear_production_commands()


@pytest.mark.anyio
@pytest.mark.parametrize("rejection", ["queue"])
async def test_rejected_mass_enrollment_allows_corrected_retry(
	monkeypatch: pytest.MonkeyPatch, rejection: str,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", AsyncMock())
	monkeypatch.setattr(command_lifecycle, "COMMAND_LIMIT", 0)
	command = StoreItemCommand(request_id=f"mass-rejected-{rejection}", item={
		"barcode": f"mass-rejected-{rejection}", "name": "Bottle",
		"dimensions": {"width": 100, "height": 100, "length": 100},
		"tare_weight": 100, "net_weight": 250, "full_net_weight": 500,
	})
	async with AsyncSessionLocal() as db:
		with pytest.raises(HTTPException) as rejected:
			await robot_router.robot_commands(
				command, worker=worker, current_user=SimpleNamespace(id=1), db=db,
			)
		assert rejected.value.status_code == 429
		# Even committing the request session must not persist rejected enrollment.
		await db.commit()
	async with AsyncSessionLocal() as db:
		assert (await db.execute(select(Item).where(Item.barcode == command.item["barcode"]))).scalar_one_or_none() is None
		assert (await db.execute(select(RobotCommand).where(RobotCommand.runtime == "production"))).scalars().all() == []
	assert worker.enqueued == []

	monkeypatch.setattr(command_lifecycle, "COMMAND_LIMIT", COMMAND_LIMIT)
	corrected = StoreItemCommand(request_id=command.request_id, item={**command.item, "net_weight": 300})
	async with AsyncSessionLocal() as db:
		accepted = await robot_router.robot_commands(
			corrected, worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
	async with AsyncSessionLocal() as db:
		item = (await db.execute(select(Item).where(Item.barcode == command.item["barcode"]))).scalar_one()
		assert item.weight == 400 and item.net_weight == 300
		assert item.tare_weight == 100 and item.full_net_weight == 500
		assert item.enrollment_weight_pending
		row = await db.get(RobotCommand, accepted.command_id)
		assert row.payload["item"]["net_weight"] == 300
		assert worker.enqueued[0][0].item["net_weight"] == 300
		await db.delete(item)
		await db.commit()
	await _clear_production_commands()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["enqueue", "restart"])
async def test_accepted_mass_enrollment_is_durable_before_enqueue_and_survives_failure(
	monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", AsyncMock())
	command = StoreItemCommand(request_id=f"mass-durable-{failure}", item={
		"barcode": f"mass-durable-{failure}", "name": "Bottle",
		"dimensions": {"width": 100, "height": 100, "length": 100},
		"tare_weight": 100, "net_weight": 250, "full_net_weight": 500,
	})

	async def inspect_before_enqueue(command, *, command_id=None, visibility=None):
		async with AsyncSessionLocal() as db:
			item = (await db.execute(select(Item).where(Item.barcode == command.item["barcode"]))).scalar_one()
			assert item.weight == 350 and item.enrollment_weight_pending
			assert (await db.get(RobotCommand, command_id)).status == "queued"
		if failure == "enqueue":
			raise RuntimeError("enqueue failed")

	monkeypatch.setattr(worker, "enqueue_command", inspect_before_enqueue)
	async with AsyncSessionLocal() as db:
		if failure == "enqueue":
			with pytest.raises(RuntimeError, match="enqueue failed"):
				await robot_router.robot_commands(
					command, worker=worker, current_user=SimpleNamespace(id=1), db=db,
				)
		else:
			await robot_router.robot_commands(
				command, worker=worker, current_user=SimpleNamespace(id=1), db=db,
			)
			assert await recover_interrupted_commands() == 1
	async with AsyncSessionLocal() as db:
		replayed = await robot_router.robot_commands(
			command, worker=None, current_user=SimpleNamespace(id=1), db=db,
		)
		assert replayed.status == "failed"
		item = (await db.execute(select(Item).where(Item.barcode == command.item["barcode"]))).scalar_one()
		assert item.weight == 350 and item.net_weight == 250
		assert item.tare_weight == 100 and item.full_net_weight == 500
		assert item.enrollment_weight_pending
		await db.delete(item)
		await db.commit()
	await _clear_production_commands()


@pytest.mark.anyio
async def test_command_admission_rolls_back_enrollment_and_skips_it_on_replay() -> None:
	await _clear_production_commands()
	request_id = "mass-atomic-admission"
	payload = _store(request_id).model_dump(mode="json", exclude_none=True)
	invalid_item = Item(barcode="mass-atomic-invalid", name=None, dimensions={})
	with pytest.raises(IntegrityError):
		await admit(payload, request_id=request_id, correlation_id=None, enrollment_item=invalid_item)
	async with AsyncSessionLocal() as db:
		assert (await db.execute(select(Item).where(Item.barcode == invalid_item.barcode))).scalar_one_or_none() is None
		assert (await db.execute(select(RobotCommand).where(RobotCommand.runtime == "production"))).scalars().all() == []

	row, was_replay = await admit(payload, request_id=request_id, correlation_id=None)
	assert not was_replay
	unused_item = Item(barcode="mass-atomic-replay", name="Bottle", dimensions={})
	replayed, was_replay = await admit(
		payload, request_id=request_id, correlation_id=None, enrollment_item=unused_item,
	)
	assert was_replay and replayed.command_id == row.command_id
	async with AsyncSessionLocal() as db:
		assert (await db.execute(select(Item).where(Item.barcode == unused_item.barcode))).scalar_one_or_none() is None
	await _clear_production_commands()


@pytest.mark.anyio
@pytest.mark.parametrize("identity", [{}, {"name": "Legacy bottle"}])
async def test_legacy_store_without_mass_preserves_identity_fallback(
	monkeypatch: pytest.MonkeyPatch, identity: dict,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(robot_router, "capture_store_observer_measurement", AsyncMock())
	item_data = {"dimensions": {"width": 100, "height": 100, "length": 100}, **identity}
	async with AsyncSessionLocal() as db:
		items_before = (await db.execute(select(Item.__table__))).all()
		accepted = await robot_router.robot_commands(
			StoreItemCommand(item=item_data, request_id="legacy-fallback"),
			worker=worker, current_user=SimpleNamespace(id=1), db=db,
		)
		assert accepted.status == "queued"
		assert len(worker.enqueued) == 1
		assert worker.enqueued[0][0].item == item_data
		assert (await db.execute(select(Item.__table__))).all() == items_before
	await _clear_production_commands()


@pytest.mark.anyio
async def test_production_command_is_durable_idempotent_and_pollable(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())

	async with AsyncSessionLocal() as db:
		accepted = await robot_router.robot_commands(
			_store("production-store-once"),
			worker=worker,  # type: ignore[arg-type]
			current_user=SimpleNamespace(id=1),
			db=db,
		)
		assert accepted.status == "queued"
		assert worker.enqueued[0][1] == accepted.command_id

		replayed = await robot_router.robot_commands(
			_store("production-store-once"),
			worker=worker,  # type: ignore[arg-type]
			current_user=SimpleNamespace(id=1),
			db=db,
		)
		assert replayed.command_id == accepted.command_id
		assert len(worker.enqueued) == 1

		with pytest.raises(HTTPException) as conflict:
			await robot_router.robot_commands(
				_store("production-store-once", name="Different payload"),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
		assert conflict.value.status_code == 409

	command, command_id, visibility = worker.enqueued[0]
	queued = await robot_router.get_robot_command(accepted.command_id)
	operation = QueuedOperation(
		command=command,
		timestamp=queued.created_at,
		command_id=command_id,
		visibility=visibility,
	)
	await handle_worker_lifecycle("running", operation, None)
	running = await robot_router.get_robot_command(accepted.command_id)
	assert running.status == "running"
	assert running.request_id == "production-store-once"

	result = {
		"message": "success",
		"command": "store_item",
		"command_id": accepted.command_id,
		"request_id": "production-store-once",
	}
	await handle_worker_lifecycle("succeeded", operation, result)
	completed = await robot_router.get_robot_command(accepted.command_id)
	assert completed.status == "succeeded"
	assert completed.result == result
	assert completed.completed_at is not None
	await _clear_production_commands()


@pytest.mark.anyio
async def test_production_requested_location_is_validated_and_canonicalized(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	worker._robot = SimpleNamespace(_grid=_store_grid(occupied={1}))  # type: ignore[attr-defined]
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	location = {"side": "left_module", "shelf_idx": 0, "cell_idx": 0}

	async with AsyncSessionLocal() as db:
		accepted = await robot_router.robot_commands(
			_store_with_location("requested-cell", "880000000111", location),
			worker=worker,  # type: ignore[arg-type]
			current_user=SimpleNamespace(id=1),
			db=db,
		)
		assert accepted.status == "queued"
		enqueued = worker.enqueued[0][0]
		assert enqueued.location is None
		assert enqueued.item["location"] == location
		row = (
			await db.execute(select(RobotCommand).where(RobotCommand.command_id == accepted.command_id))
		).scalar_one()
		assert row.payload["item"]["location"] == location
		assert "location" not in row.payload

		replayed = await robot_router.robot_commands(
			_store_with_location("requested-cell", "880000000111", location),
			worker=worker,  # type: ignore[arg-type]
			current_user=SimpleNamespace(id=1),
			db=db,
		)
		assert replayed.command_id == accepted.command_id
		assert len(worker.enqueued) == 1

		with pytest.raises(HTTPException) as conflict:
			await robot_router.robot_commands(
				_store_with_location(
					"requested-cell",
					"880000000111",
					{"side": "left_module", "shelf_idx": 0, "cell_idx": 1},
				),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
		assert conflict.value.status_code == 409

		with pytest.raises(HTTPException) as unavailable:
			await robot_router.robot_commands(
				_store_with_location(
					"occupied-cell",
					"880000000112",
					{"side": "left_module", "shelf_idx": 0, "cell_idx": 1},
				),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
		assert unavailable.value.status_code == 409
		assert unavailable.value.detail["code"] == "cell_unavailable"

		with pytest.raises(HTTPException) as missing:
			await robot_router.robot_commands(
				_store_with_location(
					"missing-cell",
					"880000000113",
					{"side": "left_module", "shelf_idx": 5, "cell_idx": 0},
				),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
		assert missing.value.status_code == 422
		assert missing.value.detail["code"] == "cell_not_found"

		with pytest.raises(HTTPException) as too_small:
			request = _store_with_location(
				"too-large-item", "880000000114", {"side": "left_module", "shelf_idx": 0, "cell_idx": 0}
			)
			item = dict(request.item or {})
			item["dimensions"] = {"width": 1000, "height": 20, "length": 30}
			await robot_router.robot_commands(
				StoreItemCommand(request_id=request.request_id, location=request.location, item=item),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
		assert too_small.value.status_code == 409
		assert too_small.value.detail["code"] == "cell_too_small"

		assert len(worker.enqueued) == 1

	await _clear_production_commands()


@pytest.mark.anyio
async def test_production_queue_limit_and_restart_recovery(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())

	async with AsyncSessionLocal() as session:
		for index in range(COMMAND_LIMIT):
			session.add(
				RobotCommand(
					command_id=f"10000000-0000-0000-0000-{index:012d}",
					runtime="production",
					payload={"command": "store_item", "item": {"barcode": str(index)}},
					status="queued",
					visibility="public",
				)
			)
		await session.commit()

	async with AsyncSessionLocal() as db:
		with pytest.raises(HTTPException) as full:
			await robot_router.robot_commands(
				_store("queue-overflow"),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
	assert full.value.status_code == 429
	assert full.value.headers == {"Retry-After": "1"}

	assert await recover_interrupted_commands() == COMMAND_LIMIT
	async with AsyncSessionLocal() as session:
		rows = (
			await session.execute(
				select(RobotCommand).where(RobotCommand.runtime == "production")
			)
		).scalars().all()
	assert all(row.status == "failed" for row in rows)
	assert all(row.result and row.result["code"] == "service_restarted" for row in rows)
	await _clear_production_commands()


@pytest.mark.anyio
async def test_production_request_retention_and_validation(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()


	worker = RecordingWorker()
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", AsyncMock())
	old_command_id = "20000000-0000-0000-0000-000000000001"
	async with AsyncSessionLocal() as session:
		session.add(
			RobotCommand(
				command_id=old_command_id,
				runtime="production",
				request_id="expired-request",
				payload=_store("expired-request").model_dump(mode="json", exclude_none=True),
				status="succeeded",
				visibility="public",
				completed_at=datetime.now(timezone.utc) - timedelta(days=8),
			)
		)
		await session.commit()

	async with AsyncSessionLocal() as db:
		accepted = await robot_router.robot_commands(
			_store("expired-request"),
			worker=worker,  # type: ignore[arg-type]
			current_user=SimpleNamespace(id=1),
			db=db,
		)
		assert accepted.command_id != old_command_id

		with pytest.raises(HTTPException) as invalid:
			await robot_router.robot_commands(
				_store(""),
				worker=worker,  # type: ignore[arg-type]
				current_user=SimpleNamespace(id=1),
				db=db,
			)
	assert invalid.value.status_code == 422
	await _clear_production_commands()


@pytest.mark.anyio
async def test_request_ids_are_unique_per_runtime() -> None:
	request_id = "runtime-scoped-request"
	payload = _store(request_id).model_dump(mode="json", exclude_none=True)
	await _clear_production_commands()
	async with AsyncSessionLocal() as session:
		await session.execute(
			delete(RobotCommand).where(
				RobotCommand.runtime == "mock",
				RobotCommand.request_id == request_id,
			)
		)
		await session.commit()

	try:
		production, production_replay = await admit(
			payload,
			request_id=request_id,
			correlation_id=None,
		)
		assert production_replay is False
		mock_runtime = MockRobotRuntime(execution_seconds=0)
		mock, mock_replay = await mock_runtime.enqueue(
			payload,
			request_id=request_id,
			correlation_id=None,
		)
		assert mock_replay is False
		assert production.command_id != mock.command_id

		assert (await admit(payload, request_id=request_id, correlation_id=None))[0].command_id == production.command_id
		assert (await mock_runtime.enqueue(payload, request_id=request_id, correlation_id=None))[0].command_id == mock.command_id
		with pytest.raises(CommandRequestConflictError):
			await admit({**payload, "item": {**payload["item"], "name": "Changed"}}, request_id=request_id, correlation_id=None)
		with pytest.raises(MockRequestConflictError):
			await mock_runtime.enqueue(
				{**payload, "item": {**payload["item"], "name": "Changed"}},
				request_id=request_id,
				correlation_id=None,
			)

		async with AsyncSessionLocal() as session:
			session.add(
				RobotCommand(
					command_id="30000000-0000-0000-0000-000000000001",
					runtime="mock",
					request_id=request_id,
					payload=payload,
					status="queued",
					visibility="public",
				)
			)
			with pytest.raises(IntegrityError):
				await session.commit()
			await session.rollback()
	finally:
		await _clear_production_commands()
		async with AsyncSessionLocal() as session:
			await session.execute(
				delete(RobotCommand).where(
					RobotCommand.runtime == "mock",
					RobotCommand.request_id == request_id,
				)
			)
			await session.commit()


@pytest.mark.anyio
async def test_internal_bin_motion_is_durable_and_marked_internal() -> None:
	await _clear_production_commands()
	worker = RecordingWorker()
	command = RetrieveItemCommand(
		request_id="bin-workflow-correlation",
		barcode="990000000001",
		location={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
	)
	row = await enqueue_internal(worker, command)
	assert row.runtime == "production"
	assert row.request_id is None
	assert row.payload["request_id"] == "bin-workflow-correlation"
	assert row.visibility == "internal"
	assert worker.enqueued[0][1:] == (row.command_id, "internal")
	await _clear_production_commands()


@pytest.mark.anyio
async def test_production_replay_precedes_worker_and_laser_preconditions(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	await _clear_production_commands()
	command = _store("replay-while-unavailable")
	payload = command.model_dump(mode="json", exclude_none=True)
	row, _ = await admit(
		payload,
		request_id=command.request_id,
		correlation_id=None,
	)
	laser_check = AsyncMock(side_effect=HTTPException(status_code=409, detail="calibrating"))
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.production)
	monkeypatch.setattr(robot_router, "_ensure_laser_auto_calibration_not_running", laser_check)

	async with AsyncSessionLocal() as db:
		replayed = await robot_router.robot_commands(
			command,
			worker=None,
			current_user=SimpleNamespace(id=1),
			db=db,
		)
	assert replayed.command_id == row.command_id
	assert replayed.status == "queued"
	laser_check.assert_not_awaited()
	await _clear_production_commands()


@pytest.mark.anyio
async def test_mock_command_replay_returns_existing_command(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	command = _store("mock-replay-during-bin-workflow")
	row = SimpleNamespace(command_id="mock-command-id", status="running")
	runtime = SimpleNamespace(replay=AsyncMock(return_value=row))
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.mock)
	monkeypatch.setattr(robot_router, "get_mock_robot_runtime", lambda: runtime)

	async with AsyncSessionLocal() as db:
		replayed = await robot_router.robot_commands(
			command,
			worker=None,
			current_user=SimpleNamespace(id=1),
			db=db,
		)
	assert replayed.command_id == "mock-command-id"
	assert replayed.status == "running"
