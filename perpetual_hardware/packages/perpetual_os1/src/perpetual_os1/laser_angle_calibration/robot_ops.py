from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .constants import GAP_DEPTH_THRESHOLD_MM
from .status import print_status, raise_if_cancelled, should_skip_motion_cleanup_on_cancel


async def run_with_temp_robot_config(
	*,
	config: Any,
	config_path: str,
	did_run: Any,
	shutdown_action: str,
) -> Any:
	import perpetual_os1.common as common_module
	import perpetual_os1.robot as robot_module
	from perpetual_os1.robot import Robot
	import perpetual_os1.laser_angle_calibration as angle_module

	with tempfile.NamedTemporaryFile(
		mode='w',
		suffix='.json',
		prefix='laser-angle-config-',
		dir=str(Path(config_path).resolve().parent),
		delete=False,
	) as temp_config_file:
		temp_config_path = Path(temp_config_file.name)
		json.dump(angle_module.build_standalone_config(config), temp_config_file, indent='\t')

	original_common_config_path = common_module.CONFIG_PATH
	original_robot_config_path = robot_module.CONFIG_PATH
	common_module.CONFIG_PATH = temp_config_path
	robot_module.CONFIG_PATH = temp_config_path

	try:
		robot = Robot()
		print_status('phase=robot-configure')
		await robot.configure()

		try:
			return await did_run(robot)
		finally:
			if should_skip_motion_cleanup_on_cancel() is False:
				if shutdown_action == 'stop':
					print_status('phase=robot-stop')
					await robot.stop()
				else:
					print_status('phase=robot-shutdown')
					await robot.shutdown()
	finally:
		common_module.CONFIG_PATH = original_common_config_path
		robot_module.CONFIG_PATH = original_robot_config_path
		temp_config_path.unlink(missing_ok=True)


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
