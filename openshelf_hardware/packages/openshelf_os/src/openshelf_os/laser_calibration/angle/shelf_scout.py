from __future__ import annotations

from typing import Any, Callable

from openshelf_os.laser_calibration.core import (
	COARSE_SCAN_STYLE_FLYING,
	get_coarse_scan_bin_mode,
	get_coarse_scan_rpm,
	get_coarse_scan_style,
)
from openshelf_os.laser_calibration.flying import scan_shelf_scout_line_flying
from openshelf_os.laser_calibration.shelf_scout import (
	build_shelf_edge_stop_predicate,
	build_shelf_scout_x_positions as build_shared_shelf_scout_x_positions,
	filter_shelf_edges as filter_shared_shelf_edges,
	scan_shelf_scout_line as scan_shared_shelf_scout_line,
	SHELF_SCOUT_POST_EDGE_SKIP_MM,
	SHELF_SCOUT_STEP_MM,
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

	diagnostics = await y_motor.get_brake_output_diagnostics()
	phase = 'scan_end' if is_enabled is True else 'scan_start'
	print_status(
		f"y_brake=drive_controlled phase:{phase} "
		f"function:0x{diagnostics['register_function']:02X} "
		f"output_state:0x{diagnostics['output_state']:04X}"
	)


def build_shelf_scout_x_positions(
	*,
	scan_config: Any,
	x_min_mm: float,
	x_max_mm: float,
	side_name: str,
	scout_x_mm: float | None,
) -> list[float]:
	resolved_x_min_mm = float(x_min_mm)
	resolved_x_max_mm = float(x_max_mm)
	resolved_scout_x_mm = scout_x_mm

	if resolved_scout_x_mm is None:
		configured_scout_x_mm = getattr(scan_config, f'{side_name}_shelf_scout_x_mm', None)

		if configured_scout_x_mm is not None:
			resolved_scout_x_mm = float(configured_scout_x_mm)

	if resolved_scout_x_mm is None:
		configured_x_min_mm = getattr(scan_config, f'{side_name}_bounds_x_min_mm', None)
		configured_x_max_mm = getattr(scan_config, f'{side_name}_bounds_x_max_mm', None)

		if configured_x_min_mm is not None and configured_x_max_mm is not None:
			resolved_x_min_mm = float(configured_x_min_mm)
			resolved_x_max_mm = float(configured_x_max_mm)

	return build_shared_shelf_scout_x_positions(
		x_min_mm=resolved_x_min_mm,
		x_max_mm=resolved_x_max_mm,
		is_right_side=side_name == 'right',
		scout_x_mm=resolved_scout_x_mm,
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
	post_edge_skip_mm: float = SHELF_SCOUT_POST_EDGE_SKIP_MM,
	scan_config: Any = None,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[list[float | None], list[ShelfSurfaceEdge]]:
	if scan_config is not None and get_coarse_scan_style(scan_config) == COARSE_SCAN_STYLE_FLYING:
		stop_when = None

		if stop_after_first_edge is True:
			stop_when = build_shelf_edge_stop_predicate(grid_step_mm=SHELF_SCOUT_STEP_MM)

		depth_values, edges, _line = await scan_shelf_scout_line_flying(
			tool,
			x_mm=x_mm,
			y_positions=y_positions,
			rpm=get_coarse_scan_rpm(scan_config),
			progress_label=progress_label,
			post_edge_skip_mm=post_edge_skip_mm,
			should_abort=should_abort,
			bin_mode=get_coarse_scan_bin_mode(scan_config),
			progress_every=progress_every,
			stop_after_first_edge=stop_after_first_edge,
			stop_when=stop_when,
		)

		return depth_values, edges

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
		post_edge_skip_mm=post_edge_skip_mm,
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
