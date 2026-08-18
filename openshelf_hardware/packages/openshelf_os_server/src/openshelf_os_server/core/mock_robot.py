from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select

from openshelf_os_server.database.models import Item, ItemLocation, MockRobotCommand
from openshelf_os_server.database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

MOCK_COMMAND_LIMIT = 32
MOCK_COMMAND_RETENTION_DAYS = 7
MOCK_EXECUTION_SECONDS = 1.0
MOCK_SIDES = ("left_module", "right_module")
MOCK_SHELVES_PER_SIDE = 4
MOCK_CELLS_PER_SHELF = 10
MOCK_CELL_DIMENSIONS = {"width": 300.0, "height": 300.0, "length": 300.0}
ACTIVE_STATUSES = ("queued", "running")


class MockQueueFullError(Exception):
	pass


class MockRequestConflictError(Exception):
	pass


class MockCommandExecutionError(Exception):
	pass


def _utcnow() -> datetime:
	return datetime.now(timezone.utc)


def _command_response(row: MockRobotCommand) -> dict[str, Any]:
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


def _deterministic_weight(upca: str) -> float:
	digest = hashlib.sha256(upca.encode("utf-8")).digest()
	return float(100 + int.from_bytes(digest[:2], "big") % 4901)


def _fits(dimensions: dict[str, Any]) -> bool:
	try:
		return all(
			0 < float(dimensions[key]) <= MOCK_CELL_DIMENSIONS[key]
			for key in ("width", "height", "length")
		)
	except (KeyError, TypeError, ValueError):
		return False


class MockRobotRuntime:
	"""One durable FIFO worker that simulates the cabinet without hardware."""

	def __init__(self, execution_seconds: float = MOCK_EXECUTION_SECONDS) -> None:
		self.execution_seconds = execution_seconds
		self._wake = asyncio.Event()
		self._task: asyncio.Task[None] | None = None
		self._lock = asyncio.Lock()
		self._status_subscribers: list[asyncio.Queue[str]] = []
		self._event_subscribers: list[asyncio.Queue[str]] = []

	async def start(self) -> None:
		async with AsyncSessionLocal() as session:
			now = _utcnow()
			result = await session.execute(
				select(MockRobotCommand).where(MockRobotCommand.status.in_(ACTIVE_STATUSES))
			)
			for row in result.scalars():
				row.status = "failed"
				row.result = {
					"message": "failed",
					"error": "Command interrupted by service restart",
					"code": "service_restarted",
				}
				row.completed_at = now

			cutoff = now - timedelta(days=MOCK_COMMAND_RETENTION_DAYS)
			await session.execute(
				delete(MockRobotCommand).where(
					MockRobotCommand.completed_at.isnot(None),
					MockRobotCommand.completed_at < cutoff,
				)
			)
			await session.commit()

		self._task = asyncio.create_task(self._run(), name="mock-robot-worker")
		await self.publish_status()

	async def stop(self) -> None:
		if self._task is not None:
			self._task.cancel()
			try:
				await self._task
			except asyncio.CancelledError:
				pass
			self._task = None

	async def enqueue(
		self,
		payload: dict[str, Any],
		*,
		request_id: str | None,
		correlation_id: str | None,
	) -> tuple[MockRobotCommand, bool]:
		if request_id is not None and (request_id.strip() == "" or len(request_id) > 255):
			raise ValueError("request_id must contain 1 to 255 characters")

		async with self._lock:
			async with AsyncSessionLocal() as session:
				await session.execute(
					delete(MockRobotCommand).where(
						MockRobotCommand.completed_at.isnot(None),
						MockRobotCommand.completed_at
						< _utcnow() - timedelta(days=MOCK_COMMAND_RETENTION_DAYS),
					)
				)
				await session.commit()
				if request_id is not None:
					result = await session.execute(
						select(MockRobotCommand).where(MockRobotCommand.request_id == request_id)
					)
					existing = result.scalar_one_or_none()
					if existing is not None:
						if existing.payload != payload:
							raise MockRequestConflictError
						return existing, True

				active_count = await session.scalar(
					select(func.count())
					.select_from(MockRobotCommand)
					.where(MockRobotCommand.status.in_(ACTIVE_STATUSES))
				)
				if int(active_count or 0) >= MOCK_COMMAND_LIMIT:
					raise MockQueueFullError

				row = MockRobotCommand(
					command_id=str(uuid.uuid4()),
					request_id=request_id,
					payload=payload,
					status="queued",
					correlation_id=correlation_id,
					created_at=_utcnow(),
				)
				session.add(row)
				await session.commit()
				await session.refresh(row)

		self._wake.set()
		await self.publish_status()
		return row, False

	async def get_command(self, command_id: str) -> MockRobotCommand | None:
		async with AsyncSessionLocal() as session:
			return await session.get(MockRobotCommand, command_id)

	async def replay(self, payload: dict[str, Any], request_id: str | None) -> MockRobotCommand | None:
		if request_id is None:
			return None
		async with AsyncSessionLocal() as session:
			await session.execute(
				delete(MockRobotCommand).where(
					MockRobotCommand.completed_at.isnot(None),
					MockRobotCommand.completed_at
					< _utcnow() - timedelta(days=MOCK_COMMAND_RETENTION_DAYS),
				)
			)
			await session.commit()
			result = await session.execute(
				select(MockRobotCommand).where(MockRobotCommand.request_id == request_id)
			)
			row = result.scalar_one_or_none()
			if row is not None and row.payload != payload:
				raise MockRequestConflictError
			return row

	async def _run(self) -> None:
		while True:
			self._wake.clear()
			row = await self._claim_next()
			if row is None:
				await self._wake.wait()
				continue

			await self.publish_status()
			try:
				await asyncio.sleep(self.execution_seconds)
				result = await self._execute(row.command_id, row.payload)
				await self._finish(row.command_id, "succeeded", result)
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				logger.info("mock command failed: %s", exc)
				result = {
					"message": "failed",
					"command": row.payload.get("command"),
					"error": str(exc),
					"code": "execution_conflict",
				}
				await self._finish(row.command_id, "failed", result)
			await self.publish_event({**result, "command_id": row.command_id})
			await self.publish_status()

	async def _claim_next(self) -> MockRobotCommand | None:
		async with self._lock:
			async with AsyncSessionLocal() as session:
				result = await session.execute(
					select(MockRobotCommand)
					.where(MockRobotCommand.status == "queued")
					.order_by(MockRobotCommand.created_at, MockRobotCommand.command_id)
					.limit(1)
				)
				row = result.scalar_one_or_none()
				if row is None:
					return None
				row.status = "running"
				row.started_at = _utcnow()
				await session.commit()
				await session.refresh(row)
				return row

	async def _finish(self, command_id: str, status: str, result: dict[str, Any]) -> None:
		async with AsyncSessionLocal() as session:
			row = await session.get(MockRobotCommand, command_id)
			if row is None:
				return
			row.status = status
			row.result = result
			row.completed_at = _utcnow()
			await session.commit()

	async def _execute(self, command_id: str, payload: dict[str, Any]) -> dict[str, Any]:
		if payload.get("command") == "store_item":
			return await self._execute_store(command_id, payload)
		if payload.get("command") == "retrieve_item":
			return await self._execute_retrieve(command_id, payload)
		raise MockCommandExecutionError("Unsupported mock command")

	async def _execute_store(self, command_id: str, payload: dict[str, Any]) -> dict[str, Any]:
		item_payload = payload.get("item")
		if not isinstance(item_payload, dict):
			raise MockCommandExecutionError("item is required")
		upca = str(item_payload.get("upca") or "")
		if upca == "":
			raise MockCommandExecutionError("item.upca is required")

		async with AsyncSessionLocal() as session:
			item = (
				await session.execute(select(Item).where(Item.upca == upca))
			).scalar_one_or_none()
			dimensions: Any
			if item is not None:
				existing = (
					await session.execute(select(ItemLocation).where(ItemLocation.item_id == item.id))
				).scalars().first()
				if existing is not None:
					raise MockCommandExecutionError(
						"Item is already stored; retrieve it before storing again"
					)
				dimensions = item.dimensions
			else:
				dimensions = item_payload.get("dimensions")

			if not isinstance(dimensions, dict) or not _fits(dimensions):
				raise MockCommandExecutionError("No suitable storage location found")

			occupied_result = await session.execute(
				select(ItemLocation.side, ItemLocation.shelf_idx, ItemLocation.cell_idx)
			)
			occupied = {(row.side, row.shelf_idx, row.cell_idx) for row in occupied_result}
			location: tuple[str, int, int] | None = None
			for side in MOCK_SIDES:
				for shelf_idx in range(MOCK_SHELVES_PER_SIDE):
					for cell_idx in range(MOCK_CELLS_PER_SHELF):
						if (side, shelf_idx, cell_idx) not in occupied:
							location = (side, shelf_idx, cell_idx)
							break
					if location is not None:
						break
				if location is not None:
					break
			if location is None:
				raise MockCommandExecutionError("No suitable storage location found")

			weight = item.weight if item is not None else item_payload.get("weight")
			if weight is None:
				weight = _deterministic_weight(upca)
			if item is None:
				item = Item(
					upca=upca,
					name=str(item_payload.get("name") or upca),
					dimensions=dimensions,
					weight=float(weight),
					item_metadata=item_payload.get("metadata") or {},
					access_level_id=item_payload.get("access_level_id"),
				)
				session.add(item)
				await session.flush()
			elif item.weight is None:
				item.weight = float(weight)

			side, shelf_idx, cell_idx = location
			session.add(
				ItemLocation(
					item_id=item.id,
					side=side,
					shelf_idx=shelf_idx,
					cell_idx=cell_idx,
				)
			)
			await session.commit()

		return {
			"message": "success",
			"command": "store_item",
			"command_id": command_id,
			"upca": upca,
			"location": {"side": side, "shelf_idx": shelf_idx, "cell_idx": cell_idx},
			"weight": float(weight),
		}

	async def _execute_retrieve(self, command_id: str, payload: dict[str, Any]) -> dict[str, Any]:
		location = payload.get("location")
		if not isinstance(location, dict):
			raise MockCommandExecutionError("Invalid location")
		side = location.get("side")
		shelf_idx = location.get("shelf_idx")
		cell_idx = location.get("cell_idx")

		async with AsyncSessionLocal() as session:
			result = await session.execute(
				select(ItemLocation, Item)
				.join(Item, Item.id == ItemLocation.item_id)
				.where(
					ItemLocation.side == side,
					ItemLocation.shelf_idx == shelf_idx,
					ItemLocation.cell_idx == cell_idx,
				)
			)
			row = result.first()
			if row is None:
				raise MockCommandExecutionError("No item found at requested location")
			item_location, item = row
			await session.delete(item_location)
			await session.commit()

		return {
			"message": "success",
			"command": "retrieve_item",
			"command_id": command_id,
			"upca": item.upca,
			"location": {"side": side, "shelf_idx": shelf_idx, "cell_idx": cell_idx},
		}

	async def status_payload(self) -> str:
		async with AsyncSessionLocal() as session:
			commands = (
				await session.execute(
					select(MockRobotCommand)
					.where(MockRobotCommand.status.in_(ACTIVE_STATUSES))
					.order_by(MockRobotCommand.created_at, MockRobotCommand.command_id)
				)
			).scalars().all()
			occupied_rows = await session.execute(
				select(ItemLocation.side, ItemLocation.shelf_idx, ItemLocation.cell_idx)
			)
			occupied = {(row.side, row.shelf_idx, row.cell_idx) for row in occupied_rows}

		running = next((row for row in commands if row.status == "running"), None)
		queued = [row for row in commands if row.status == "queued"]
		axis = {"position": 0, "homed": 1, "hard_stopped": 0, "status_flags": {"value": 0}, "error_flags": {"value": 0}}
		storage: dict[str, list[dict[str, Any]]] = {}
		for side in MOCK_SIDES:
			storage[side] = [
				{
					"shelf_idx": shelf_idx,
					"cells": [
						{
							"cell_idx": cell_idx,
							"status": "occupied" if (side, shelf_idx, cell_idx) in occupied else "available",
							"dimensions": MOCK_CELL_DIMENSIONS,
						}
						for cell_idx in range(MOCK_CELLS_PER_SHELF)
					],
				}
				for shelf_idx in range(MOCK_SHELVES_PER_SIDE)
			]

		payload = {
			"mode": "mock",
			"running": True,
			"ready": True,
			"current_command": running.payload if running is not None else None,
			"queued_operations": [
				{"command_id": row.command_id, "command": row.payload, "timestamp": row.created_at.isoformat()}
				for row in queued
			],
			"queue_length": len(queued),
			"error_queue": [],
			"is_crashed": False,
			"needs_home": False,
			"robot_status": {
				"gantry": {"x_axis": axis, "y_axis": axis},
				"tool": {"z_axis": axis, "stage": axis, "rotate": axis},
				"state": {"current_cell": None},
				"storage": storage,
				"cabinet_doors": {"left": "closed", "right": "closed"},
				"io_door_open": False,
				"safety": {"ready": True},
			},
		}
		return json.dumps(payload)

	async def publish_status(self) -> None:
		payload = await self.status_payload()
		for queue in list(self._status_subscribers):
			await queue.put(payload)

	async def publish_event(self, payload: dict[str, Any]) -> None:
		message = json.dumps(payload)
		for queue in list(self._event_subscribers):
			await queue.put(message)

	async def subscribe_status(self) -> tuple[asyncio.Queue[str], str]:
		queue: asyncio.Queue[str] = asyncio.Queue()
		self._status_subscribers.append(queue)
		return queue, await self.status_payload()

	async def unsubscribe_status(self, queue: asyncio.Queue[str]) -> None:
		if queue in self._status_subscribers:
			self._status_subscribers.remove(queue)

	async def subscribe_events(self) -> asyncio.Queue[str]:
		queue: asyncio.Queue[str] = asyncio.Queue()
		self._event_subscribers.append(queue)
		return queue

	async def unsubscribe_events(self, queue: asyncio.Queue[str]) -> None:
		if queue in self._event_subscribers:
			self._event_subscribers.remove(queue)


mock_robot_runtime: MockRobotRuntime | None = None


async def start_mock_robot_runtime() -> MockRobotRuntime:
	global mock_robot_runtime
	if mock_robot_runtime is None:
		mock_robot_runtime = MockRobotRuntime()
		await mock_robot_runtime.start()
	return mock_robot_runtime


async def stop_mock_robot_runtime() -> None:
	global mock_robot_runtime
	if mock_robot_runtime is not None:
		await mock_robot_runtime.stop()
		mock_robot_runtime = None


def get_mock_robot_runtime() -> MockRobotRuntime:
	if mock_robot_runtime is None:
		raise ValueError("Mock robot runtime is not initialized")
	return mock_robot_runtime


def serialize_mock_command(row: MockRobotCommand) -> dict[str, Any]:
	return _command_response(row)
