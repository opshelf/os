from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

SHELF_SCOUT_STEP_MM = 2.0
SHELF_SCOUT_X_OFFSETS_MM = [0.0]
BACK_MODULE_SHELF_SCOUT_X_MM = 0.0
SHELF_EDGE_CONFIRMATION_COUNT = 3
SHELF_BRIM_MIN_HEIGHT_MM = 8.0
SHELF_BRIM_MAX_HEIGHT_MM = 13.0
SHELF_BRIM_HEIGHT_MM = 14.0
SHELF_MIN_SPACING_MM = 40.0
SHELF_EDGE_TOP_MARGIN_MM = 5.0
SHELF_SCOUT_POST_EDGE_SKIP_MM = 40.0
GAP_DEPTH_THRESHOLD_MM = 0.01


@dataclass
class ShelfSurfaceEdge:
	x_mm: float
	y_mm: float


SampleDepth = Callable[..., Awaitable[float | None]]
StatusPrinter = Callable[[str], None]


def depth_to_label(depth_mm: float | None) -> str:
	if depth_mm is None:
		return 'none'

	return f'{depth_mm:.2f}'


def is_positive_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm > GAP_DEPTH_THRESHOLD_MM


def raise_if_cancelled(should_abort: Callable[[], bool] | None = None) -> None:
	if should_abort is not None and should_abort() is True:
		raise asyncio.CancelledError()

	current_task = asyncio.current_task()

	if current_task is not None and current_task.cancelling() > 0:
		raise asyncio.CancelledError()


def build_shelf_scout_x_positions(
	*,
	x_min_mm: float,
	x_max_mm: float,
	is_back_side: bool,
	scout_x_mm: float | None = None,
) -> list[float]:
	if scout_x_mm is not None:
		return [float(scout_x_mm)]

	if is_back_side is True:
		return [BACK_MODULE_SHELF_SCOUT_X_MM]

	positions: list[float] = []

	for offset_mm in SHELF_SCOUT_X_OFFSETS_MM:
		x_mm = max(x_min_mm, x_max_mm - offset_mm)

		if x_mm in positions:
			continue

		positions.append(x_mm)

	return positions


def build_shelf_edge_for_transition(
	*,
	x_mm: float,
	transition_y_mm: float,
	positive_run_length: int,
	brim_height_mm: float = SHELF_BRIM_HEIGHT_MM,
	min_positive_run_length: int = SHELF_EDGE_CONFIRMATION_COUNT,
) -> ShelfSurfaceEdge | None:
	if positive_run_length < min_positive_run_length:
		return None

	center_y_mm = float(transition_y_mm) - (float(brim_height_mm) / 2.0)
	return ShelfSurfaceEdge(x_mm=x_mm, y_mm=center_y_mm)


def filter_shelf_edges(
	*,
	edges: list[ShelfSurfaceEdge],
	y_max_mm: float,
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


async def scan_shelf_scout_line(
	tool: Any,
	*,
	x_mm: float,
	y_positions: list[float],
	settle_ms: int,
	samples_per_point: int,
	progress_label: str,
	sample_depth: SampleDepth,
	print_status: StatusPrinter | None = None,
	stop_after_first_edge: bool = False,
	progress_every: int = 25,
	post_edge_skip_mm: float = SHELF_SCOUT_POST_EDGE_SKIP_MM,
	brim_max_height_mm: float = SHELF_BRIM_MAX_HEIGHT_MM,
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
		depth_mm = await sample_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		depth_values[position_idx] = depth_mm
		sampled_count += 1

		if print_status is not None:
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

		edge = build_shelf_edge_for_transition(
			x_mm=x_mm,
			transition_y_mm=y_mm,
			positive_run_length=run_end_idx - run_start_idx + 1,
		)
		if edge is not None:
			detected_edges.append(edge)

			if stop_after_first_edge is True:
				return depth_values, detected_edges

			skip_target_y_mm = y_mm - post_edge_skip_mm

			while position_idx < len(y_positions) and y_positions[position_idx] > skip_target_y_mm:
				position_idx += 1

			run_start_idx = None
			run_end_idx = None
			continue

		run_start_idx = None
		run_end_idx = None
		position_idx += 1

	return depth_values, detected_edges
