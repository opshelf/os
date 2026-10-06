from __future__ import annotations

import argparse
from typing import Any, Callable

import openshelf_os.laser_calibration.angle as angle_module
from openshelf_os.laser_calibration.shelf_scout import filter_shelf_edges as filter_shared_shelf_edges
from .constants import (
	FRONT_MODULE_ROTATION_CENTER_DEG,
	BACK_MODULE_ROTATION_CENTER_DEG,
	ROTATION_SAFE_X_MM,
	SHELF_SCOUT_STEP_MM,
)
from .models import ShelfSurfaceEdge
from .robot_ops import move_gantry_direct
from .status import print_status, raise_if_cancelled


class ShelfScoutError(ValueError):
	def __init__(self, message: str, debug_document: dict[str, Any]) -> None:
		super().__init__(message)
		self.debug_document = debug_document


def build_shelf_scout_debug_samples(
	*,
	y_positions: list[float],
	depth_values: list[float | None],
) -> list[dict[str, float | None]]:
	samples: list[dict[str, float | None]] = []

	for position_idx, y_mm in enumerate(y_positions):
		depth_mm = depth_values[position_idx] if position_idx < len(depth_values) else None
		samples.append(
			{
				'y_mm': float(y_mm),
				'depth_mm': None if depth_mm is None else float(depth_mm),
			}
		)

	return samples


def build_shelf_scout_debug_document(
	*,
	side_name: str,
	y_min_mm: float,
	y_max_mm: float,
	scout_x_positions: list[float],
	scout_y_positions: list[float],
	rotation_center_deg: float,
	stage_extension_mm: float,
	base_z_mm: float,
	settle_ms: int,
	samples_per_point: int,
	lines: list[dict[str, Any]],
	edges: list[ShelfSurfaceEdge],
	filtered_edges: list[ShelfSurfaceEdge],
) -> dict[str, Any]:
	return {
		'phase': 'shelf-scout',
		'side': side_name,
		'y_range': [float(y_min_mm), float(y_max_mm)],
		'scout_x_positions': [float(value) for value in scout_x_positions],
		'scout_y_count': len(scout_y_positions),
		'scout_y_first_mm': float(scout_y_positions[0]) if len(scout_y_positions) > 0 else None,
		'scout_y_last_mm': float(scout_y_positions[-1]) if len(scout_y_positions) > 0 else None,
		'rotation_center_deg': float(rotation_center_deg),
		'stage_extension_mm': float(stage_extension_mm),
		'base_z_mm': float(base_z_mm),
		'settle_ms': int(settle_ms),
		'samples_per_point': int(samples_per_point),
		'detected_edges': [
			{'x_mm': float(edge.x_mm), 'y_mm': float(edge.y_mm)}
			for edge in edges
		],
		'filtered_edges': [
			{'x_mm': float(edge.x_mm), 'y_mm': float(edge.y_mm)}
			for edge in filtered_edges
		],
		'lines': lines,
	}


def get_default_rotation_center_deg(side_name: str) -> float:
	if side_name == 'back':
		return BACK_MODULE_ROTATION_CENTER_DEG

	return FRONT_MODULE_ROTATION_CENTER_DEG


def resolve_rotation_center_deg(side_name: str, parameters: Any) -> float:
	parameter_name = 'ROTATION_BACK' if side_name == 'back' else 'ROTATION_FRONT'

	if isinstance(parameters, dict):
		value = parameters.get(parameter_name)
	else:
		value = getattr(parameters, parameter_name, None)

	if value is None:
		raise ValueError(f'{parameter_name} is required for {side_name} module angle calibration')

	return float(value)


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
		should_use_safe_back_rotation_path=True,
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
	should_use_safe_back_rotation_path: bool,
	should_move_z: bool,
	should_abort: Callable[[], bool] | None = None,
) -> None:
	if should_rotate is True and should_use_safe_back_rotation_path is True:
		if await tool.is_rotation_at_position(absolute_rotation_deg):
			await move_gantry_direct(
				tool,
				x_mm=x_mm,
				y_mm=y_mm,
				should_abort=should_abort,
			)
		else:
			await move_gantry_direct(
				tool,
				x_mm=ROTATION_SAFE_X_MM,
				y_mm=y_mm,
				should_abort=should_abort,
			)
			original_x = await tool.ensure_safe_rotation_x_position()
			try:
				await tool.ensure_safe_rotation_stage_extension()
				await tool._mr.move_to_position(absolute_rotation_deg)
			finally:
				await tool.restore_safe_rotation_x_position(original_x)
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

		if should_rotate is True and await tool.is_rotation_at_position(absolute_rotation_deg) is False:
			original_x = await tool.ensure_safe_rotation_x_position()
			try:
				await tool.ensure_safe_rotation_stage_extension()
				await tool._mr.move_to_position(absolute_rotation_deg)
			finally:
				await tool.restore_safe_rotation_x_position(original_x)

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
	scout_lines: list[dict[str, Any]] = []
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
					should_use_safe_back_rotation_path=False,
					should_move_z=False,
					should_abort=should_abort,
				)
				raise_if_cancelled(should_abort)
			print_status(
				f'phase=shelf-scout scout={scout_idx}/{len(scout_x_positions)} x={float(scout_x_mm):.2f}'
			)
			depth_values, scout_edges = await angle_module.scan_shelf_scout_line(
				tool,
				x_mm=float(scout_x_mm),
				y_positions=scout_y_positions,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_label=f'shelf_scout_{scout_idx}',
				stop_after_first_edge=True,
				should_abort=should_abort,
			)
			scout_lines.append(
				{
					'scout_idx': scout_idx,
					'x_mm': float(scout_x_mm),
					'detected_edges': [
						{'x_mm': float(edge.x_mm), 'y_mm': float(edge.y_mm)}
						for edge in scout_edges
					],
					'samples': build_shelf_scout_debug_samples(
						y_positions=scout_y_positions,
						depth_values=depth_values,
					),
				}
			)
			edges.extend(scout_edges)
			raise_if_cancelled(should_abort)

			if len(scout_edges) > 0:
				break
	finally:
		if is_y_brake_disabled is True:
			await angle_module.set_y_brake_for_shelf_scouting(tool, is_enabled=True)

	filtered_edges = filter_shared_shelf_edges(
		edges=edges,
		y_max_mm=y_max_mm,
	)
	print_status(
		'shelf_edges='
		f'{len(filtered_edges)} '
		f'y={[round(edge.y_mm, 2) for edge in filtered_edges]}'
	)

	if len(filtered_edges) == 0:
		debug_document = build_shelf_scout_debug_document(
			side_name=side_name,
			y_min_mm=y_min_mm,
			y_max_mm=y_max_mm,
			scout_x_positions=scout_x_positions,
			scout_y_positions=scout_y_positions,
			rotation_center_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			base_z_mm=base_z_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			lines=scout_lines,
			edges=edges,
			filtered_edges=filtered_edges,
		)
		raise ShelfScoutError('no shelf surface detected during scout', debug_document)

	target_y_mm = float(filtered_edges[0].y_mm)
	print_status(f'angle_capture_y_mm={target_y_mm:.2f}')
	return target_y_mm
