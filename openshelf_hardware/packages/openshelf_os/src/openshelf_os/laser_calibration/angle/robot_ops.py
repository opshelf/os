from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

import openshelf_os.laser_calibration.angle as angle_module
from openshelf_os.laser_calibration.core import run_with_temp_robot_config as run_with_temp_robot_config_shared

from .constants import GAP_DEPTH_THRESHOLD_MM
from .status import print_status, raise_if_cancelled


async def run_with_temp_robot_config(
	*,
	config: Any,
	config_path: str,
	did_run: Any,
	shutdown_action: str,
) -> Any:
	return await run_with_temp_robot_config_shared(
		config=config,
		config_path=config_path,
		didRun=did_run,
		shutdown_action=shutdown_action,
		config_builder=angle_module.build_standalone_config,
		status_print=print_status,
		prefix="laser-angle-config-",
	)


async def sample_laser_depth(
	tool: Any,
	*,
	settle_ms: int,
	samples_per_point: int,
) -> float | None:
	if settle_ms > 0:
		await asyncio.sleep(settle_ms / 1000.0)

	sample_values: list[float] = []

	for _ in range(samples_per_point):
		depth_mm = await tool.get_laser_distance()

		if depth_mm is None:
			continue

		if float(depth_mm) <= GAP_DEPTH_THRESHOLD_MM:
			continue

		sample_values.append(float(depth_mm))

	if len(sample_values) == 0:
		return None

	sample_values.sort()
	return sample_values[len(sample_values) // 2]

async def move_gantry_direct(
	tool: Any,
	*,
	x_mm: float,
	y_mm: float,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	await tool._my.move_to_position_no_wait(y_mm)
	await tool._mx.move_to_position_no_wait(x_mm)
	await tool.gantry_wait(should_abort=should_abort)


async def probe_stage_until_stall(
	tool: Any,
	*,
	target_stage_mm: float,
	retract_stage_mm: float,
	settle_ms: int,
	timeout_s: float,
	probe_rpm: int | None,
	no_progress_s: float,
	probe_step_mm: float,
	fast_approach_mm: float,
	fast_approach_rpm: int | None,
	auto_retract: bool = True,
	should_use_continuous_move: bool = False,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[bool, float, str, int | None]:
	raise_if_cancelled(should_abort)
	await tool._ms.move_to_position(retract_stage_mm)
	await tool._ms.clear_alarm()

	if settle_ms > 0:
		await asyncio.sleep(settle_ms / 1000.0)

	start_time = time.monotonic()
	current_target_mm = retract_stage_mm
	fast_target_mm = min(max(fast_approach_mm, retract_stage_mm), target_stage_mm)

	if fast_target_mm > retract_stage_mm:
		raise_if_cancelled(should_abort)
		try:
			await tool._ms.move_to_position(fast_target_mm, rpm=fast_approach_rpm)
		except Exception:
			await asyncio.sleep(0.03)
			stage_position_mm = await tool._ms.get_position()
			alarm_code = await tool._ms.get_param('Current alarm')
			await tool._ms.clear_alarm()

			if auto_retract is True:
				await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

			return True, float(stage_position_mm), 'fast_approach_fault', alarm_code

		current_target_mm = fast_target_mm

	position_epsilon_mm = 0.02
	target_tolerance_mm = max(0.35, float(probe_step_mm) * 0.75)
	step_mm = target_stage_mm - retract_stage_mm if should_use_continuous_move is True else probe_step_mm

	while current_target_mm < target_stage_mm:
		raise_if_cancelled(should_abort)
		next_target_mm = min(current_target_mm + step_mm, target_stage_mm)
		await tool._ms.move_to_position_no_wait(next_target_mm, rpm=probe_rpm)
		last_movement_time = time.monotonic()
		last_position_mm = await tool._ms.get_position()
		segment_start_position_mm = last_position_mm
		has_moved = False

		while True:
			raise_if_cancelled(should_abort)
			await asyncio.sleep(0.03)
			now = time.monotonic()
			stage_position_mm = await tool._ms.get_position()
			alarm_code = await tool._ms.get_param('Current alarm')

			if abs(stage_position_mm - last_position_mm) > position_epsilon_mm:
				last_movement_time = now
				has_moved = True

			last_position_mm = stage_position_mm

			if alarm_code is not None and alarm_code != 0:
				await tool._ms.stop()
				await asyncio.sleep(0.03)
				stage_position_mm = await tool._ms.get_position()
				await tool._ms.clear_alarm()

				if auto_retract is True:
					await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

				return True, float(stage_position_mm), 'alarm', int(alarm_code)

			if stage_position_mm >= next_target_mm - target_tolerance_mm:
				current_target_mm = next_target_mm
				break

			is_static_after_command = abs(stage_position_mm - segment_start_position_mm) <= position_epsilon_mm
			if now - last_movement_time > no_progress_s and (has_moved is True or is_static_after_command is True):
				await tool._ms.stop()
				await asyncio.sleep(0.03)
				stage_position_mm = await tool._ms.get_position()
				await tool._ms.clear_alarm()

				if auto_retract is True:
					await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

				return True, float(stage_position_mm), 'no_progress', alarm_code

			if now - start_time > timeout_s:
				await tool._ms.stop()
				await asyncio.sleep(0.03)
				stage_position_mm = await tool._ms.get_position()
				await tool._ms.clear_alarm()

				if auto_retract is True:
					await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

				return True, float(stage_position_mm), 'timeout', alarm_code

	stage_position_mm = await tool._ms.get_position()

	if auto_retract is True:
		await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)

	return False, float(stage_position_mm), 'limit', None


async def probe_z_until_stall(
	tool: Any,
	*,
	target_z_mm: float,
	retract_z_mm: float,
	settle_ms: int,
	timeout_s: float,
	probe_rpm: int | None,
	no_progress_s: float,
	probe_step_mm: float,
	fast_approach_mm: float,
	fast_approach_rpm: int | None,
	auto_retract: bool = True,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[bool, float, str, int | None]:
	"""Creep the Z/gripper axis (``tool._mg``) toward a wall until it gently stalls.

	Direct mirror of :func:`probe_stage_until_stall` for the Z axis. Retracts, optionally
	fast-approaches to a standoff, then steps in by ``probe_step_mm`` at low ``probe_rpm``
	and stops the instant the motor reports an alarm (contact), stops making progress, or
	times out, then auto-retracts. ``target_z_mm`` is a HARD CAP: if no stall occurs the
	probe stops there and returns ``'limit'`` without extending further. Returns
	``(stalled, z_position_mm, reason, alarm_code)``.
	"""
	raise_if_cancelled(should_abort)
	await tool._mg.move_to_position(retract_z_mm)
	await tool._mg.clear_alarm()

	if settle_ms > 0:
		await asyncio.sleep(settle_ms / 1000.0)

	start_time = time.monotonic()
	current_target_mm = retract_z_mm
	fast_target_mm = min(max(fast_approach_mm, retract_z_mm), target_z_mm)

	if fast_target_mm > retract_z_mm:
		raise_if_cancelled(should_abort)
		try:
			await tool._mg.move_to_position(fast_target_mm, rpm=fast_approach_rpm)
		except Exception:
			await asyncio.sleep(0.03)
			z_position_mm = await tool._mg.get_position()
			alarm_code = await tool._mg.get_param('Current alarm')
			await tool._mg.clear_alarm()

			if auto_retract is True:
				await tool._mg.move_to_position(retract_z_mm, rpm=probe_rpm)

			# A fault during the RAPID approach is an obstruction, not a wall: the fast
			# segment stops short of any wall by design, so nothing should be in the way.
			# Reporting it as a stall records the collision itself as a contact point.
			return False, float(z_position_mm), 'fast_approach_fault', alarm_code

		current_target_mm = fast_target_mm

	position_epsilon_mm = 0.02
	target_tolerance_mm = max(0.35, float(probe_step_mm) * 0.75)

	while current_target_mm < target_z_mm:
		raise_if_cancelled(should_abort)
		next_target_mm = min(current_target_mm + probe_step_mm, target_z_mm)
		await tool._mg.move_to_position_no_wait(next_target_mm, rpm=probe_rpm)
		last_movement_time = time.monotonic()
		last_position_mm = await tool._mg.get_position()
		segment_start_position_mm = last_position_mm
		has_moved = False

		while True:
			raise_if_cancelled(should_abort)
			await asyncio.sleep(0.03)
			now = time.monotonic()
			z_position_mm = await tool._mg.get_position()
			alarm_code = await tool._mg.get_param('Current alarm')

			if abs(z_position_mm - last_position_mm) > position_epsilon_mm:
				last_movement_time = now
				has_moved = True

			last_position_mm = z_position_mm

			if alarm_code is not None and alarm_code != 0:
				await tool._mg.stop()
				await asyncio.sleep(0.03)
				z_position_mm = await tool._mg.get_position()
				await tool._mg.clear_alarm()

				if auto_retract is True:
					await tool._mg.move_to_position(retract_z_mm, rpm=probe_rpm)

				return True, float(z_position_mm), 'alarm', int(alarm_code)

			if z_position_mm >= next_target_mm - target_tolerance_mm:
				current_target_mm = next_target_mm
				break

			is_static_after_command = abs(z_position_mm - segment_start_position_mm) <= position_epsilon_mm
			if now - last_movement_time > no_progress_s and (has_moved is True or is_static_after_command is True):
				await tool._mg.stop()
				await asyncio.sleep(0.03)
				z_position_mm = await tool._mg.get_position()
				await tool._mg.clear_alarm()

				if auto_retract is True:
					await tool._mg.move_to_position(retract_z_mm, rpm=probe_rpm)

				return True, float(z_position_mm), 'no_progress', alarm_code

			if now - start_time > timeout_s:
				await tool._mg.stop()
				await asyncio.sleep(0.03)
				z_position_mm = await tool._mg.get_position()
				await tool._mg.clear_alarm()

				if auto_retract is True:
					await tool._mg.move_to_position(retract_z_mm, rpm=probe_rpm)

				return False, float(z_position_mm), 'timeout', alarm_code

	z_position_mm = await tool._mg.get_position()

	if auto_retract is True:
		await tool._mg.move_to_position(retract_z_mm, rpm=probe_rpm)

	return False, float(z_position_mm), 'limit', None
