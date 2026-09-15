from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, cast

from fastapi import HTTPException, status

_motion_lock = asyncio.Lock()
_current_operation: str | None = None

def _conflict_detail(message: str, *, reason: str | None = None) -> dict[str, str]:
	detail = {"message": message, "code": "movement_in_progress"}
	if reason is not None:
		detail["reason"] = reason
	return detail


def get_manual_motion_operation() -> str | None:
	if _motion_lock.locked() is not True:
		return None

	return _current_operation or "manual_motion"


def _should_wait_for_probe(operation: str) -> bool:
	return operation != "motor_probe" and _current_operation == "motor_probe"


def _has_pending_commands(worker_state: dict[str, Any]) -> bool:
	command_queue = worker_state.get("_command_queue")
	qsize = getattr(command_queue, "qsize", None)
	if callable(qsize) is True:
		try:
			qsize_fn = cast(Callable[[], int], qsize)
			if qsize_fn() > 0:
				return True
		except Exception:
			pass

	queued_commands_value = worker_state.get("_queued_commands")
	current_operation = worker_state.get("_current_operation")
	if isinstance(queued_commands_value, list) is False:
		return False

	queued_commands = cast(list[Any], queued_commands_value)
	return any(operation is not current_operation for operation in queued_commands)


def ensure_robot_available_for_manual_motion(worker) -> None:
	worker_state = cast(dict[str, Any], getattr(worker, "__dict__", {}))
	current_command = worker_state.get("_current_command")
	is_demo_cycling = worker_state.get("_demo_cycling", False)
	has_pending_commands = _has_pending_commands(worker_state)
	is_safety_blocking = (
		worker_state.get("_safety_guard_bypassed") is not True
		and (
			bool(worker_state.get("_safety_motion_blocked"))
			or bool(worker_state.get("_awaiting_safety_ack"))
			or bool(worker_state.get("_safety_active_sources"))
		)
	)

	if is_safety_blocking is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Robot is paused by a safety interruption",
				reason="safety_interruption",
			),
		)
	if current_command is not None:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Robot is busy processing another command",
				reason="robot_command_in_progress",
			),
		)

	if has_pending_commands is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Robot has queued commands",
				reason="robot_command_queued",
			),
		)


	if is_demo_cycling is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Demo cycle is already running",
				reason="demo_cycle_in_progress",
			),
		)


@asynccontextmanager
async def manual_motion_guard(
	operation: str,
	worker=None,
) -> AsyncIterator[None]:
	global _current_operation

	if worker is not None:
		ensure_robot_available_for_manual_motion(worker)

	if _motion_lock.locked() is True and _should_wait_for_probe(operation) is not True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Movement already in progress",
				reason=_current_operation,
			),
		)

	await _motion_lock.acquire()

	try:
		if worker is not None:
			ensure_robot_available_for_manual_motion(worker)

		_current_operation = operation
		yield
	finally:
		_current_operation = None
		_motion_lock.release()


@asynccontextmanager
async def robot_command_enqueue_guard() -> AsyncIterator[None]:
	global _current_operation

	if _motion_lock.locked() is True and _should_wait_for_probe("robot_command_enqueue") is not True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Movement already in progress",
				reason=_current_operation,
			),
		)

	await _motion_lock.acquire()
	_current_operation = "robot_command_enqueue"

	try:
		yield
	finally:
		_current_operation = None
		_motion_lock.release()
