from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, cast

from fastapi import HTTPException, status

from openshelf_os.robot.worker import TRAY_ON_TOOL_CODE, TRAY_ON_TOOL_ERROR
from openshelf_os_server.errors import api_error

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
				"The robot is busy with another task.",
				reason="robot_command_in_progress",
			),
		)

	if has_pending_commands is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"The robot has other tasks waiting.",
				reason="robot_command_queued",
			),
		)


	if is_demo_cycling is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"The robot is running the demo cycle.",
				reason="demo_cycle_in_progress",
			),
		)


async def ensure_no_tray_on_tool(worker, *, read_gripper: bool = True) -> None:
	"""Reject motion while the gripper holds a tray. By default always re-reads the gripper: flows
	outside the command queue (demo, calibration tests) can leave a tray held without updating the
	cached flag. read_gripper=False reads it only when the cache says held, so a clear tool passes;
	queued commands are gated by the worker, and a read while one carries a tray would misreport it."""
	was_held = getattr(worker, "_tray_on_tool", False) is True
	if read_gripper is False and was_held is False:
		return
	is_held = await worker.refresh_tray_on_tool()
	if is_held is not was_held:
		await worker.get_status_snapshot(force_refresh=True)
	if is_held is False:
		return
	raise api_error(status.HTTP_409_CONFLICT, TRAY_ON_TOOL_CODE, TRAY_ON_TOOL_ERROR)


@asynccontextmanager
async def manual_motion_guard(
	operation: str,
	worker=None,
	*,
	allow_tray_on_tool: bool = False,
) -> AsyncIterator[None]:
	"""allow_tray_on_tool admits the actions that recover a held tray: operator-driven moves and jogs,
	release, homing, clearing alarms, power cycling, probing and unlocking the cabinet. Automated
	sequences (demo, test cycles, grip probe) stay blocked."""
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
			if allow_tray_on_tool is False:
				await ensure_no_tray_on_tool(worker)

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
