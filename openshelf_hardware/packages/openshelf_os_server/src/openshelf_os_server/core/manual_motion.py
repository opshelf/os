from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator, Callable, NoReturn, cast

from fastapi import HTTPException, status

from openshelf_os.robot.worker import RobotWorker, TRAY_ON_TOOL_CODE
from openshelf_os_server.errors import api_error, error_detail

_motion_lock = asyncio.Lock()
_current_operation: str | None = None

def _conflict_detail(message: str, *, reason: str | None = None) -> dict[str, Any]:
	extra = {} if reason is None else {"reason": reason}
	return error_detail(message, "movement_in_progress", **extra)


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


def _is_motion_interlock_blocked(worker) -> bool:
	robot = getattr(worker, "_robot", None)
	motion_safety = getattr(robot, "_motion_safety", None)
	return getattr(motion_safety, "blocked", False) is True


def _raise_motion_interlock_conflict(worker_state: dict[str, Any]) -> NoReturn:
	if worker_state.get("_is_estop_active") is True:
		raise api_error(status.HTTP_409_CONFLICT, "emergency_stop", "")
	if worker_state.get("_is_crashed") is True:
		raise api_error(status.HTTP_409_CONFLICT, "robot_crashed", "")
	if worker_state.get("_is_emergency_stopped") is True:
		raise api_error(status.HTTP_409_CONFLICT, "home_required", "")
	raise HTTPException(
		status_code=status.HTTP_409_CONFLICT,
		detail=_conflict_detail(
			"Robot is paused by a safety interruption",
			reason="safety_interruption",
		),
	)


def ensure_robot_available_for_manual_motion(
	worker,
	*,
	allow_safety_interruption: bool = False,
	allow_doors_interruption: bool = False,
	allow_motion_interlock: bool = False,
	allow_stopped_interlock: bool = False,
) -> None:
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

	is_doors_only = all(
		source == "cabinet_doors" for source in worker_state.get("_safety_active_sources") or []
	)
	if (
		is_safety_blocking is True
		and allow_safety_interruption is False
		and (allow_doors_interruption is False or is_doors_only is False)
	):
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"Robot is paused by a safety interruption",
				reason="safety_interruption",
			),
		)

	if (
		worker_state.get("_is_estop_active") is True
		and allow_safety_interruption is False
		and allow_motion_interlock is False
		and allow_doors_interruption is False
	):
		raise api_error(status.HTTP_409_CONFLICT, "emergency_stop", "")

	if (
		_is_motion_interlock_blocked(worker) is True
		and allow_safety_interruption is False
		and allow_motion_interlock is False
		and allow_stopped_interlock is False
		and allow_doors_interruption is False
	):
		_raise_motion_interlock_conflict(worker_state)

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
	raise api_error(status.HTTP_409_CONFLICT, TRAY_ON_TOOL_CODE, "")


@asynccontextmanager
async def manual_motion_guard(
	operation: str,
	worker=None,
	*,
	allow_tray_on_tool: bool = False,
	allow_doors_interruption: bool = False,
	allow_motion_interlock: bool = False,
	allow_stopped_recovery: bool = False,
) -> AsyncIterator[None]:
	"""allow_tray_on_tool admits the actions that recover a held tray: single-motor moves and the IO door
	from the debug motor controls, release, homing, clearing alarms, power cycling, probing and unlocking
	the cabinet. Calibration moves and jogs and automated sequences stay blocked.

	allow_doors_interruption admits actions that move no gantry axis while the open cabinet doors
	hold the interlock: the stop recovery unlocks the cabinet and opens the gripper fingers with the
	doors open, so the operator can support the tray. Any other active source, or a running or
	queued command, still refuses them.

	allow_motion_interlock admits non-motion diagnostic/recovery actions (probing, clearing alarms,
	power cycling) that never wait on the hardware motion interlock.

	allow_stopped_recovery admits manual homing routes while the robot is crashed or emergency-stopped,
	evaluating safety guards and temporarily releasing the motion interlock just as a queued home does."""
	global _current_operation

	if worker is not None:
		ensure_robot_available_for_manual_motion(
			worker,
			allow_doors_interruption=allow_doors_interruption,
			allow_motion_interlock=allow_motion_interlock,
			allow_stopped_interlock=allow_stopped_recovery,
		)

	if _motion_lock.locked() is True and _should_wait_for_probe(operation) is not True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_conflict_detail(
				"",
				reason=_current_operation,
			),
		)

	await _motion_lock.acquire()

	try:
		async with AsyncExitStack() as stack:
			if worker is not None:
				if allow_stopped_recovery is True and isinstance(worker, RobotWorker):
					ensure_robot_available_for_manual_motion(
						worker,
						allow_doors_interruption=allow_doors_interruption,
						allow_motion_interlock=allow_motion_interlock,
						allow_stopped_interlock=True,
					)
					await stack.enter_async_context(worker.manual_recovery_motion())
				ensure_robot_available_for_manual_motion(
					worker,
					allow_doors_interruption=allow_doors_interruption,
					allow_motion_interlock=allow_motion_interlock,
				)
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
				"",
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
