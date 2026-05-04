from __future__ import annotations

from typing import Any, Callable

from perpetual_os1.laser_shelf_scout import (
	build_shelf_scout_x_positions as build_shared_shelf_scout_x_positions,
	filter_shelf_edges as filter_shared_shelf_edges,
	scan_shelf_scout_line as scan_shared_shelf_scout_line,
)

from .models import ShelfSurfaceEdge
from .robot_ops import sample_laser_depth
from .status import print_status


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
	left_reference_x_mm: float | None = None

	if scout_x_mm is None:
		left_reference = getattr(scan_config, 'left_reference', None)

		if left_reference is not None:
			left_reference_x_mm = float(left_reference.x_mm)

	return build_shared_shelf_scout_x_positions(
		x_min_mm=x_min_mm,
		x_max_mm=x_max_mm,
		is_right_side=side_name == 'right',
		scout_x_mm=scout_x_mm,
		left_reference_x_mm=left_reference_x_mm,
	)


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
	return await scan_shared_shelf_scout_line(
		tool,
		x_mm=x_mm,
		y_positions=y_positions,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		progress_label=progress_label,
		sample_depth=sample_laser_depth,
		print_status=print_status,
		stop_after_first_edge=stop_after_first_edge,
		progress_every=progress_every,
		should_abort=should_abort,
	)


def filter_shelf_edges(
	*,
	edges: list[ShelfSurfaceEdge],
	y_min_mm: float,
	y_max_mm: float,
	side_name: str,
	scout_x_positions: list[float],
) -> list[ShelfSurfaceEdge]:
	return filter_shared_shelf_edges(
		edges=edges,
		y_max_mm=y_max_mm,
	)
