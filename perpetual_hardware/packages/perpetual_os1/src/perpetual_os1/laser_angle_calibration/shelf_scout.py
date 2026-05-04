from __future__ import annotations

from typing import Any, Callable

from .constants import (
	RIGHT_MODULE_SHELF_SCOUT_X_MM,
	SHELF_BRIM_MAX_HEIGHT_MM,
	SHELF_BRIM_MIN_HEIGHT_MM,
	SHELF_EDGE_CONFIRMATION_COUNT,
	SHELF_EDGE_TOP_MARGIN_MM,
	SHELF_MIN_SPACING_MM,
	SHELF_SCOUT_POST_EDGE_SKIP_MM,
	SHELF_SCOUT_STEP_MM,
	SHELF_SCOUT_X_OFFSETS_MM,
)
from .models import ShelfSurfaceEdge
from .robot_ops import sample_laser_depth
from .status import print_status, raise_if_cancelled
from .utils import depth_to_label, is_positive_depth


async def set_y_brake_for_shelf_scouting(
	tool: Any,
	*,
	is_enabled: bool,
) -> None:
	y_motor = getattr(tool, '_my', None)

	if y_motor is None:
		raise ValueError('y-axis motor is required for shelf scouting')

	if is_enabled is True:
		await y_motor.restore_brake_output()
		print_status('y_brake=enabled')
		return

	await y_motor.disable_brake_output()
	print_status('y_brake=disabled')


def build_shelf_scout_x_positions(
	*,
	scan_config: Any,
	x_min_mm: float,
	x_max_mm: float,
	side_name: str,
	scout_x_mm: float | None,
) -> list[float]:
	if scout_x_mm is not None:
		return [float(scout_x_mm)]

	if side_name == 'right':
		return [RIGHT_MODULE_SHELF_SCOUT_X_MM]

	left_reference = getattr(scan_config, 'left_reference', None)

	if left_reference is not None:
		return [max(float(left_reference.x_mm), x_max_mm)]

	positions: list[float] = []

	for offset_mm in SHELF_SCOUT_X_OFFSETS_MM:
		x_mm = max(x_min_mm, x_max_mm - offset_mm)

		if x_mm in positions:
			continue

		positions.append(x_mm)

	return positions

async def scan_shelf_scout_line(
	tool: Any,
	*,
	x_mm: float,
	y_positions: list[float],
	settle_ms: int,
	samples_per_point: int,
	progress_label: str,
	stop_after_first_edge: bool = False,
	progress_every: int = 25,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[list[float | None], list[ShelfSurfaceEdge]]:
	if len(y_positions) == 0:
		return [], []

	raise_if_cancelled(should_abort)
	await tool._mx.move_to_position_no_wait(x_mm)
	await tool._my.move_to_position_no_wait(y_positions[0])
	await tool.gantry_wait(should_abort=should_abort)

	depth_values: list[float | None] = [None for _ in y_positions]
	detected_edges: list[ShelfSurfaceEdge] = []
	position_idx = 0
	sampled_count = 0
	run_start_idx: int | None = None
	run_end_idx: int | None = None

	while position_idx < len(y_positions):
		raise_if_cancelled(should_abort)
		y_mm = y_positions[position_idx]

		if position_idx > 0:
			await tool._my.move_to_position_no_wait(y_mm)
			await tool.gantry_wait(should_abort=should_abort)

		raise_if_cancelled(should_abort)
		depth_mm = await sample_laser_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		depth_values[position_idx] = depth_mm
		sampled_count += 1

		if sampled_count % progress_every == 0 or position_idx + 1 == len(y_positions):
			print_status(
				f'{progress_label}={sampled_count}/{len(y_positions)} '
				f'x={x_mm:.2f} '
				f'y={y_mm:.2f} '
				f'depth={depth_to_label(depth_mm)}'
			)

		if is_positive_depth(depth_mm) is True:
			if run_start_idx is None:
				run_start_idx = position_idx

			run_end_idx = position_idx
			position_idx += 1
			continue

		if run_start_idx is None or run_end_idx is None:
			position_idx += 1
			continue

		run_length = run_end_idx - run_start_idx + 1
		band_height_mm = abs(y_positions[run_end_idx] - y_positions[run_start_idx]) + SHELF_SCOUT_STEP_MM
		is_bounded_band = run_start_idx > 0

		if (
			run_length >= SHELF_EDGE_CONFIRMATION_COUNT and
			is_bounded_band is True and
			band_height_mm >= SHELF_BRIM_MIN_HEIGHT_MM and
			band_height_mm <= SHELF_BRIM_MAX_HEIGHT_MM
		):
			center_y_mm = float((y_positions[run_start_idx] + y_positions[run_end_idx]) / 2.0)
			detected_edges.append(
				ShelfSurfaceEdge(
					x_mm=x_mm,
					y_mm=center_y_mm,
				)
			)

			if stop_after_first_edge is True:
				return depth_values, detected_edges

			skip_target_y_mm = y_positions[run_end_idx] - SHELF_SCOUT_POST_EDGE_SKIP_MM

			while position_idx < len(y_positions) and y_positions[position_idx] > skip_target_y_mm:
				position_idx += 1

			run_start_idx = None
			run_end_idx = None
			continue

		run_start_idx = None
		run_end_idx = None
		position_idx += 1

	if run_start_idx is not None and run_end_idx is not None:
		run_length = run_end_idx - run_start_idx + 1
		band_height_mm = abs(y_positions[run_end_idx] - y_positions[run_start_idx]) + SHELF_SCOUT_STEP_MM
		is_bounded_band = run_start_idx > 0 and run_end_idx < len(y_positions) - 1

		if (
			run_length >= SHELF_EDGE_CONFIRMATION_COUNT and
			is_bounded_band is True and
			band_height_mm >= SHELF_BRIM_MIN_HEIGHT_MM and
			band_height_mm <= SHELF_BRIM_MAX_HEIGHT_MM
		):
			center_y_mm = float((y_positions[run_start_idx] + y_positions[run_end_idx]) / 2.0)
			detected_edges.append(
				ShelfSurfaceEdge(
					x_mm=x_mm,
					y_mm=center_y_mm,
				)
			)

			if stop_after_first_edge is True:
				return depth_values, detected_edges

	return depth_values, detected_edges

def filter_shelf_edges(
	*,
	edges: list[ShelfSurfaceEdge],
	y_min_mm: float,
	y_max_mm: float,
	side_name: str,
	scout_x_positions: list[float],
) -> list[ShelfSurfaceEdge]:
	filtered_edges: list[ShelfSurfaceEdge] = []

	for edge in sorted(edges, key=lambda current: -current.y_mm):
		if edge.y_mm > y_max_mm - SHELF_EDGE_TOP_MARGIN_MM:
			continue

		if len(filtered_edges) == 0:
			filtered_edges.append(edge)
			continue

		if abs(filtered_edges[-1].y_mm - edge.y_mm) < SHELF_MIN_SPACING_MM:
			continue

		filtered_edges.append(edge)

	return filtered_edges
