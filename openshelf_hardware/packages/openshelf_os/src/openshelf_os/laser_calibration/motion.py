# ruff: noqa: F401, F403, F405
from __future__ import annotations

from .core import *

async def get_scan_position(tool: Any) -> tuple[float, float]:
	scan_position = await tool.get_param("SCAN_POSITION")

	if scan_position is None:
		raise ValueError("SCAN_POSITION parameter is not configured")

	return float(scan_position[0]), float(scan_position[1])


async def get_laser_scan_z_extension_mm(tool: Any, scan_config: Any) -> float:
	z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")

	if z_extension_place is None:
		raise ValueError("Z_EXTENSION_PLACE parameter is not configured")

	return float(z_extension_place) + get_laser_scan_pose_z_delta_from_place_mm(scan_config)


async def ensure_scan_tool_ready(tool: Any) -> None:
	if tool._laser_enabled is False or tool._laser is None:
		raise ValueError("Laser is not enabled")

	if tool._mx is None or tool._my is None:
		raise ValueError("Gantry motors (x_axis, y_axis) are required for laser scan")

	if tool._ms is None or tool._mg is None:
		raise ValueError("Tool motors (stage, z_axis) are required for laser scan")


async def set_y_brake_for_scan_motion(
	tool: Any,
	*,
	is_enabled: bool,
) -> None:
	y_motor = getattr(tool, "_my", None)

	if y_motor is None:
		raise ValueError("Y-axis motor is required for scan motion")

	diagnostics = await y_motor.get_brake_output_diagnostics()
	phase = "scan_end" if is_enabled is True else "scan_start"
	print_status(
		f"y_brake=drive_controlled phase:{phase} "
		f"function:0x{diagnostics['register_function']:02X} "
		f"output_state:0x{diagnostics['output_state']:04X}"
	)


async def enter_scan_mode(tool: Any, side: StorageModuleSide, scan_config: Any) -> None:
	await ensure_scan_tool_ready(tool)

	side_number = 0
	target_position = "front"

	if side == StorageModuleSide.BACK:
		side_number = 1
		target_position = "back"

	stage_extension_mm = get_laser_scan_pose_stage_mm(scan_config)
	z_extension_mm = await get_laser_scan_z_extension_mm(tool, scan_config)
	z_move_rpm = await tool.get_param("Z_MOVE_RPM")
	z_move_accel = await tool.get_param("Z_MOVE_ACCEL")
	rotation_front = await tool.get_param("ROTATION_FRONT")
	rotation_back = await tool.get_param("ROTATION_BACK")

	if z_move_rpm is None or z_move_accel is None:
		raise ValueError("Z_MOVE_RPM and Z_MOVE_ACCEL parameters are required for laser scan")

	scan_x_mm, scan_y_mm = await get_scan_position(tool)

	print_status(
		f"scan_setup=module:{side.value} "
		f"scan_position:({scan_x_mm:.2f},{scan_y_mm:.2f}) "
		f"stage_extension_mm:{stage_extension_mm:.2f} "
		f"z_extension_mm:{z_extension_mm:.2f} "
		f"rotation_front:{rotation_front} "
		f"rotation_back:{rotation_back}"
	)

	await tool.retract()

	if side == StorageModuleSide.BACK:
		await tool.move(
			AxisCoordinate(x=BACK_MODULE_ROTATION_SAFE_X_MM, y=scan_y_mm, s=side_number)
		)

		rotation_before = await tool._mr.get_position()
		print_status(f"rotation_before={rotation_before}")
		await tool.rotate_to_position(target_position)
		rotation_after = await tool._mr.get_position()
		print_status(f"rotation_after={rotation_after}")
		await tool.move(AxisCoordinate(x=scan_x_mm, y=scan_y_mm, s=side_number))
	else:
		await tool.move(AxisCoordinate(x=scan_x_mm, y=scan_y_mm, s=side_number))

		rotation_before = await tool._mr.get_position()
		print_status(f"rotation_before={rotation_before}")
		await tool.rotate_to_position(target_position)
		rotation_after = await tool._mr.get_position()
		print_status(f"rotation_after={rotation_after}")
	await tool._ms.ready()
	await tool._ms.move_to_position(stage_extension_mm)
	await tool._mg.move_to_position(z_extension_mm, rpm=z_move_rpm, accel=z_move_accel)


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

		sample_values.append(float(depth_mm))

	if len(sample_values) == 0:
		return None

	sample_values.sort()
	return sample_values[len(sample_values) // 2]
