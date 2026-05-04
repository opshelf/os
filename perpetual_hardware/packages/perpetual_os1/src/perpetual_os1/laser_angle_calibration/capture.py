from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Callable

from .config import (
	get_laser_sample_settle_ms,
	get_laser_samples_per_point,
	get_laser_scan_stage_extension_mm,
	get_laser_scan_z_offset_from_place_mm,
)
from .constants import (
	LEFT_MODULE_ROTATION_CENTER_DEG,
	RIGHT_MODULE_ROTATION_CENTER_DEG,
	ROTATION_SAFE_X_MM,
	SHELF_SCOUT_STEP_MM,
)
from .geometry import build_axis_positions
from .models import LaserAngleCalibrationSample, ShelfSurfaceEdge
from .robot_ops import move_gantry_direct, run_with_temp_robot_config, sample_laser_depth
from .status import print_status, raise_if_cancelled


def get_default_rotation_center_deg(side_name: str) -> float:
	if side_name == 'right':
		return RIGHT_MODULE_ROTATION_CENTER_DEG

	return LEFT_MODULE_ROTATION_CENTER_DEG


async def enter_capture_mode(
	tool: Any,
	*,
	side_name: str,
	side_number: int,
	x_mm: float,
	y_mm: float,
	absolute_rotation_deg: float,
	stage_extension_mm: float,
	z_mm: float,
	z_move_rpm: float,
	z_move_accel: float,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	await tool.retract()

	await prepare_capture_pose(
		tool,
		side_name=side_name,
		side_number=side_number,
		x_mm=x_mm,
		y_mm=y_mm,
		absolute_rotation_deg=absolute_rotation_deg,
		stage_extension_mm=stage_extension_mm,
		z_mm=z_mm,
		z_move_rpm=z_move_rpm,
		z_move_accel=z_move_accel,
		should_rotate=True,
		should_use_safe_right_rotation_path=True,
		should_move_z=True,
		should_abort=should_abort,
	)

async def prepare_capture_pose(
	tool: Any,
	*,
	side_name: str,
	side_number: int,
	x_mm: float,
	y_mm: float,
	absolute_rotation_deg: float,
	stage_extension_mm: float,
	z_mm: float,
	z_move_rpm: float,
	z_move_accel: float,
	should_rotate: bool,
	should_use_safe_right_rotation_path: bool,
	should_move_z: bool,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	if should_rotate is True and should_use_safe_right_rotation_path is True:
		await move_gantry_direct(
			tool,
			x_mm=ROTATION_SAFE_X_MM,
			y_mm=y_mm,
			should_abort=should_abort,
		)
		await tool.ensure_safe_rotation_stage_extension()
		await tool._mr.move_to_position(absolute_rotation_deg)
		await move_gantry_direct(
			tool,
			x_mm=x_mm,
			y_mm=y_mm,
			should_abort=should_abort,
		)
	else:
		await move_gantry_direct(
			tool,
			x_mm=x_mm,
			y_mm=y_mm,
			should_abort=should_abort,
		)

		if should_rotate is True:
			await tool.ensure_safe_rotation_stage_extension()
			await tool._mr.move_to_position(absolute_rotation_deg)

	await tool._ms.ready()
	await tool._ms.move_to_position(stage_extension_mm)

	if should_move_z is True:
		await tool._mg.move_to_position(z_mm, rpm=z_move_rpm, accel=z_move_accel)


async def resolve_capture_y_mm(
	tool: Any,
	*,
	args: argparse.Namespace,
	scan_config: Any,
	side_name: str,
	side_number: int,
	x_positions: list[float],
	rotation_center_deg: float,
	stage_extension_mm: float,
	base_z_mm: float,
	z_move_rpm: float,
	z_move_accel: float,
	settle_ms: int,
	samples_per_point: int,
	should_abort: Callable[[], bool] | None = None,
) -> float:
	raise_if_cancelled(should_abort)
	import perpetual_os1.laser_angle_calibration as angle_module
	raw_y_mm = getattr(args, 'y_mm', None)
	raw_y_range = getattr(args, 'y_range', None)
	raw_scout_x_mm = getattr(args, 'scout_x_mm', None)

	if raw_y_mm is not None:
		return float(raw_y_mm)

	if raw_y_range is None:
		raise ValueError('either --y-mm or --y-range is required')

	y_min_mm = float(raw_y_range[0])
	y_max_mm = float(raw_y_range[1])
	scout_x_positions = angle_module.build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=min(x_positions),
		x_max_mm=max(x_positions),
		side_name=side_name,
		scout_x_mm=raw_scout_x_mm,
	)
	scout_y_positions = list(reversed(angle_module.build_axis_positions(y_min_mm, y_max_mm, SHELF_SCOUT_STEP_MM)))
	edges: list[ShelfSurfaceEdge] = []
	is_y_brake_disabled = False

	if len(scout_y_positions) == 0:
		raise ValueError('scout y range produced no positions')

	try:
		raise_if_cancelled(should_abort)
		await angle_module.set_y_brake_for_shelf_scouting(tool, is_enabled=False)
		is_y_brake_disabled = True
		await angle_module.enter_capture_mode(
			tool,
			side_name=side_name,
			side_number=side_number,
			x_mm=float(scout_x_positions[0]),
			y_mm=float(scout_y_positions[0]),
			absolute_rotation_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			z_mm=base_z_mm,
			z_move_rpm=z_move_rpm,
			z_move_accel=z_move_accel,
			should_abort=should_abort,
		)
		raise_if_cancelled(should_abort)

		for scout_idx, scout_x_mm in enumerate(scout_x_positions, start=1):
			raise_if_cancelled(should_abort)
			if scout_idx > 1:
				await angle_module.prepare_capture_pose(
					tool,
					side_name=side_name,
					side_number=side_number,
					x_mm=float(scout_x_mm),
					y_mm=float(scout_y_positions[0]),
					absolute_rotation_deg=rotation_center_deg,
					stage_extension_mm=stage_extension_mm,
					z_mm=base_z_mm,
					z_move_rpm=z_move_rpm,
					z_move_accel=z_move_accel,
					should_rotate=False,
					should_use_safe_right_rotation_path=False,
					should_move_z=False,
					should_abort=should_abort,
				)
				raise_if_cancelled(should_abort)
			print_status(
				f'phase=shelf-scout scout={scout_idx}/{len(scout_x_positions)} x={float(scout_x_mm):.2f}'
			)
			_, scout_edges = await angle_module.scan_shelf_scout_line(
				tool,
				x_mm=float(scout_x_mm),
				y_positions=scout_y_positions,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_label=f'shelf_scout_{scout_idx}',
				stop_after_first_edge=True,
				should_abort=should_abort,
			)
			edges.extend(scout_edges)
			raise_if_cancelled(should_abort)

			if len(scout_edges) > 0:
				break
	finally:
		if is_y_brake_disabled is True:
			await angle_module.set_y_brake_for_shelf_scouting(tool, is_enabled=True)

	filtered_edges = angle_module.filter_shelf_edges(
		edges=edges,
		y_min_mm=y_min_mm,
		y_max_mm=y_max_mm,
		side_name=side_name,
		scout_x_positions=scout_x_positions,
	)
	print_status(
		'shelf_edges='
		f'{len(filtered_edges)} '
		f'y={[round(edge.y_mm, 2) for edge in filtered_edges]}'
	)

	if len(filtered_edges) == 0:
		raise ValueError('no shelf surface detected during scout')

	target_y_mm = float(filtered_edges[0].y_mm)
	print_status(f'angle_capture_y_mm={target_y_mm:.2f}')
	return target_y_mm


async def capture_samples_with_robot(args: argparse.Namespace) -> tuple[list[LaserAngleCalibrationSample], dict[str, Any]]:
	from perpetual_os1.common import load_config_file

	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	settle_ms = int(
		args.settle_ms
		if args.settle_ms is not None
		else get_laser_sample_settle_ms(scan_config)
	)
	samples_per_point = int(
		args.samples_per_point
		if args.samples_per_point is not None
		else get_laser_samples_per_point(scan_config)
	)
	x_positions = build_axis_positions(args.x_range[0], args.x_range[1], args.x_range[2])
	z_offset_positions = build_axis_positions(
		args.z_offset_range[0],
		args.z_offset_range[1],
		args.z_offset_range[2],
	)
	rotation_positions = build_axis_positions(
		args.rotation_range[0],
		args.rotation_range[1],
		args.rotation_range[2],
	)
	stage_extension_mm = float(
		args.stage_extension_mm
		if args.stage_extension_mm is not None
		else get_laser_scan_stage_extension_mm(scan_config)
	)
	laser_scan_z_offset_from_place_mm = get_laser_scan_z_offset_from_place_mm(
		scan_config
	)
	samples: list[LaserAngleCalibrationSample] = []

	async def did_run(robot: Any) -> tuple[list[LaserAngleCalibrationSample], dict[str, Any]]:
		tool = robot._tool

		if tool._laser_enabled is False or tool._laser is None:
			raise ValueError('laser is not enabled')

		if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
			raise ValueError('x, y, z, stage, and rotation motors are required')

		side_name = args.module
		side_number = 0 if side_name == 'left' else 1
		rotation_center_deg = float(
			args.rotation_center_deg
			if args.rotation_center_deg is not None
			else get_default_rotation_center_deg(side_name)
		)

		for rotation_deg in rotation_positions:
			absolute_rotation_deg = rotation_center_deg + float(rotation_deg)

			if absolute_rotation_deg < 0.0 or absolute_rotation_deg > 180.0:
				raise ValueError(
					'rotation sweep exceeds motor range '
					f'(absolute target {absolute_rotation_deg:.3f} deg is outside 0..180)'
				)

		z_extension_place = await tool.get_param('Z_EXTENSION_PLACE')
		z_move_rpm = await tool.get_param('Z_MOVE_RPM')
		z_move_accel = await tool.get_param('Z_MOVE_ACCEL')

		if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
			raise ValueError('Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required')

		base_z_mm = float(
			args.base_z_mm
			if args.base_z_mm is not None
			else float(z_extension_place) + laser_scan_z_offset_from_place_mm
		)
		current_z_mm: float | None = None
		await tool.retract()
		capture_y_mm = await resolve_capture_y_mm(
			tool,
			args=args,
			scan_config=scan_config,
			side_name=side_name,
			side_number=side_number,
			x_positions=x_positions,
			rotation_center_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			base_z_mm=base_z_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		current_z_mm = base_z_mm
		current_absolute_rotation_deg: float | None = rotation_center_deg

		for z_offset_mm in z_offset_positions:
			target_z_mm = base_z_mm + float(z_offset_mm)

			for rotation_idx, rotation_deg in enumerate(rotation_positions, start=1):
				absolute_rotation_deg = rotation_center_deg + float(rotation_deg)

				for x_idx, x_mm in enumerate(x_positions, start=1):
					print_status(
						f'phase=capture x={x_idx}/{len(x_positions)} '
						f'z={z_offset_mm:.2f} '
						f'rotation={rotation_idx}/{len(rotation_positions)} '
						f'absolute_rotation_deg={absolute_rotation_deg:.3f}'
					)
					await prepare_capture_pose(
						tool,
						side_name=side_name,
						side_number=side_number,
						x_mm=float(x_mm),
						y_mm=capture_y_mm,
						absolute_rotation_deg=absolute_rotation_deg,
						stage_extension_mm=stage_extension_mm,
						z_mm=target_z_mm,
						z_move_rpm=float(z_move_rpm),
						z_move_accel=float(z_move_accel),
						should_rotate=current_absolute_rotation_deg != absolute_rotation_deg,
						should_use_safe_right_rotation_path=True,
						should_move_z=current_z_mm != target_z_mm,
					)
					current_z_mm = target_z_mm
					current_absolute_rotation_deg = absolute_rotation_deg
					depth_mm = await sample_laser_depth(
						tool,
						settle_ms=settle_ms,
						samples_per_point=samples_per_point,
					)

					if depth_mm is None:
						print_status(
							f'sample=invalid x={x_mm:.3f} '
							f'z={target_z_mm:.3f} '
							f'rotation_deg={rotation_deg:.3f}'
						)
						continue

					samples.append(
						LaserAngleCalibrationSample(
							axis_x_mm=float(x_mm),
							axis_z_mm=target_z_mm,
							rotation_deg=float(rotation_deg),
							absolute_rotation_deg=float(absolute_rotation_deg),
							stage_extension_mm=stage_extension_mm,
							z_extension_mm=target_z_mm,
							distance_mm=float(depth_mm),
						)
					)
					print_status(
						f'sample=ok x={x_mm:.3f} '
						f'z={target_z_mm:.3f} '
						f'rotation_deg={rotation_deg:.3f} '
						f'distance_mm={depth_mm:.3f}'
					)

		await tool.retract()

		return samples, {
			'config_path': str(Path(args.config).resolve()),
			'module': side_name,
			'y_mm': float(capture_y_mm),
			'y_range': None if args.y_range is None else [float(args.y_range[0]), float(args.y_range[1])],
			'x_positions': x_positions,
			'z_offset_positions': z_offset_positions,
			'rotation_positions': rotation_positions,
			'stage_extension_mm': stage_extension_mm,
			'base_z_mm': base_z_mm,
			'rotation_center_deg': rotation_center_deg,
			'settle_ms': settle_ms,
			'samples_per_point': samples_per_point,
		}

	return await run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		did_run=did_run,
		shutdown_action=args.shutdown_action,
	)
