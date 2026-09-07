from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from openshelf_os.robot.models import QueuedOperation
from openshelf_os_server.database.models import Item, RobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal

COMMAND_LIMIT = 32
COMMAND_RETENTION_DAYS = 7
ACTIVE_STATUSES = ("queued", "running")


class CommandQueueFullError(Exception):
	pass


class CommandRequestConflictError(Exception):
	pass


_admission_lock = asyncio.Lock()


def utcnow() -> datetime:
	return datetime.now(timezone.utc)


async def prune_expired() -> None:
	async with AsyncSessionLocal() as session:
		await session.execute(
			delete(RobotCommand).where(
				RobotCommand.runtime == "production",
				RobotCommand.completed_at.isnot(None),
				RobotCommand.completed_at < utcnow() - timedelta(days=COMMAND_RETENTION_DAYS),
			)
		)
		await session.commit()


async def recover_interrupted_commands() -> int:
	"""Fail commands that cannot safely be replayed after a process restart."""
	async with AsyncSessionLocal() as session:
		rows = (
			await session.execute(
				select(RobotCommand).where(
					RobotCommand.runtime == "production",
					RobotCommand.status.in_(ACTIVE_STATUSES),
				)
			)
		).scalars().all()
		now = utcnow()
		for row in rows:
			row.status = "failed"
			row.result = {
				"message": "failed",
				"command": row.payload.get("command"),
				"command_id": row.command_id,
				"request_id": row.payload.get("request_id") or row.request_id,
				"error": "Command interrupted by service restart",
				"code": "service_restarted",
			}
			if row.visibility == "internal":
				row.result["visibility"] = "internal"
			row.completed_at = now
		await session.commit()
	return len(rows)


async def replay(payload: dict[str, Any], request_id: str | None) -> RobotCommand | None:
	if request_id is None:
		return None
	await prune_expired()
	async with AsyncSessionLocal() as session:
		row = (
			await session.execute(
				select(RobotCommand).where(
					RobotCommand.runtime == "production",
					RobotCommand.request_id == request_id,
				)
			)
		).scalar_one_or_none()
		if row is not None and row.payload != payload:
			raise CommandRequestConflictError
		return row


async def admit(
	payload: dict[str, Any],
	*,
	request_id: str | None,
	correlation_id: str | None,
	visibility: str = "public",
	enrollment_item: Item | None = None,
) -> tuple[RobotCommand, bool]:
	if request_id is not None and (request_id.strip() == "" or len(request_id) > 255):
		raise ValueError("request_id must contain 1 to 255 characters")
	if visibility not in {"public", "internal"}:
		raise ValueError("visibility must be public or internal")

	async with _admission_lock:
		await prune_expired()
		async with AsyncSessionLocal() as session:
			if request_id is not None:
				existing = (
					await session.execute(
						select(RobotCommand).where(
							RobotCommand.runtime == "production",
							RobotCommand.request_id == request_id,
						)
					)
				).scalar_one_or_none()
				if existing is not None:
					if existing.payload != payload:
						raise CommandRequestConflictError
					return existing, True

			active_count = await session.scalar(
				select(func.count())
				.select_from(RobotCommand)
				.where(
					RobotCommand.runtime == "production",
					RobotCommand.status.in_(ACTIVE_STATUSES),
				)
			)
			if int(active_count or 0) >= COMMAND_LIMIT:
				raise CommandQueueFullError

			row = RobotCommand(
				command_id=str(uuid.uuid4()),
				runtime="production",
				request_id=request_id,
				payload=payload,
				status="queued",
				correlation_id=correlation_id,
				visibility=visibility,
				created_at=utcnow(),
			)
			session.add(row)
			if enrollment_item is not None:
				# Enrollment and command acceptance must survive or roll back together.
				session.add(enrollment_item)
			await session.commit()
			await session.refresh(row)
			return row, False


async def get_command(command_id: str) -> RobotCommand | None:
	async with AsyncSessionLocal() as session:
		return await session.get(RobotCommand, command_id)


async def fail_before_enqueue(command_id: str, error: str) -> None:
	row = await get_command(command_id)
	if row is None:
		return
	result = {
		"message": "failed",
		"command": row.payload.get("command"),
		"command_id": command_id,
		"request_id": row.payload.get("request_id") or row.request_id,
		"error": error,
		"code": "enqueue_failed",
	}
	if row.visibility == "internal":
		result["visibility"] = "internal"
	await finish_command(command_id, "failed", result)


async def handle_worker_lifecycle(
	status: str,
	operation: QueuedOperation,
	result: dict[str, Any] | None,
) -> None:
	if operation.command_id is None:
		return
	if status == "running":
		async with AsyncSessionLocal() as session:
			row = await session.get(RobotCommand, operation.command_id)
			if row is None or row.status != "queued":
				return
			row.status = "running"
			row.started_at = utcnow()
			await session.commit()
		return
	if status in {"succeeded", "failed"} and result is not None:
		await finish_command(operation.command_id, status, result)


async def finish_command(command_id: str, status: str, result: dict[str, Any]) -> None:
	async with AsyncSessionLocal() as session:
		row = await session.get(RobotCommand, command_id)
		if row is None or row.status in {"succeeded", "failed"}:
			return
		row.status = status
		row.result = result
		row.completed_at = utcnow()
		await session.commit()


def serialize_command(row: RobotCommand) -> dict[str, Any]:
	return {
		"command_id": row.command_id,
		"request_id": row.request_id,
		"command": row.payload.get("command"),
		"status": row.status,
		"result": row.result,
		"created_at": row.created_at.isoformat(),
		"started_at": row.started_at.isoformat() if row.started_at is not None else None,
		"completed_at": row.completed_at.isoformat() if row.completed_at is not None else None,
	}


async def enqueue_internal(worker: Any, command: Any) -> RobotCommand:
	"""Persist and enqueue an OS-owned motion without using partner idempotency."""
	payload = command.model_dump(mode="json", exclude_none=True)
	row, _ = await admit(
		payload,
		request_id=None,
		correlation_id=None,
		visibility="internal",
	)
	await enqueue_persisted(worker, command, row, visibility="internal")
	return row


async def enqueue_persisted(
	worker: Any,
	command: Any,
	row: RobotCommand,
	*,
	visibility: str | None = None,
) -> None:
	"""Put an already durable command on the in-memory worker queue."""
	try:
		parameters = inspect.signature(worker.enqueue_command).parameters
		if "command_id" in parameters:
			await worker.enqueue_command(
				command,
				command_id=row.command_id,
				visibility=visibility,
			)
		else:  # Lightweight service test doubles use the legacy one-argument shape.
			await worker.enqueue_command(command)
	except Exception as exc:
		await fail_before_enqueue(row.command_id, str(exc))
		raise
