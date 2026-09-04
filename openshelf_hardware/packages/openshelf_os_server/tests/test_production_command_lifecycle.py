from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from openshelf_os.operating_mode import OperatingMode
from openshelf_os.robot.commands import RetrieveItemCommand, StoreItemCommand
from openshelf_os.robot.models import QueuedOperation
from openshelf_os_server.core.command_lifecycle import (
	COMMAND_LIMIT,
	CommandRequestConflictError,
	admit,
	enqueue_internal,
	recover_interrupted_commands,
	handle_worker_lifecycle,
)
from openshelf_os_server.core.mock_robot import MockRequestConflictError, MockRobotRuntime
from openshelf_os_server.database.models import RobotCommand
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
			"upca": "880000000001",
			"name": name,
			"dimensions": {"width": 10, "height": 20, "length": 30},
		},
	)


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
					payload={"command": "store_item", "item": {"upca": str(index)}},
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
		upca="990000000001",
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
async def test_mock_replay_precedes_active_bin_workflow_check(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	command = _store("mock-replay-during-bin-workflow")
	row = SimpleNamespace(command_id="mock-command-id", status="running")
	runtime = SimpleNamespace(replay=AsyncMock(return_value=row))
	bin_check = AsyncMock(side_effect=HTTPException(status_code=423, detail="bin active"))
	monkeypatch.setattr(robot_router, "get_operating_mode", lambda: OperatingMode.mock)
	monkeypatch.setattr(robot_router, "get_mock_robot_runtime", lambda: runtime)
	monkeypatch.setattr(robot_router, "_ensure_no_active_bin_workflow", bin_check)

	async with AsyncSessionLocal() as db:
		replayed = await robot_router.robot_commands(
			command,
			worker=None,
			current_user=SimpleNamespace(id=1),
			db=db,
		)
	assert replayed.command_id == "mock-command-id"
	assert replayed.status == "running"
	bin_check.assert_not_awaited()
