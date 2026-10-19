from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

SHELF_SCOUT_STEP_MM = 2.0
SHELF_SCOUT_X_OFFSETS_MM = [0.0]
BACK_MODULE_SHELF_SCOUT_X_MM = 0.0
SHELF_EDGE_CONFIRMATION_COUNT = 3
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


def walk_shelf_edges_from_line(
	*,
	x_mm: float,
	y_positions: list[float],
	depth_values: list[float | None],
	brim_height_mm: float = SHELF_BRIM_HEIGHT_MM,
	min_positive_run_length: int = SHELF_EDGE_CONFIRMATION_COUNT,
	post_edge_skip_mm: float = 0.0,
	stop_after_first_edge: bool = False,
	is_unobserved: Callable[[int], bool] | None = None,
) -> tuple[list[ShelfSurfaceEdge], int | None]:
	"""Walk a shelf scan line, pausing on unobserved positions.

	Returns the edges found so far and the index of the first position the walk would read
	that `is_unobserved` rejects, or None once the walk completes. Positions the post-edge skip
	jumps over, or that lie past the first edge when stopping there, are never reported.
	"""
	if len(depth_values) != len(y_positions):
		raise ValueError("depth_values length does not match y_positions")

	descending = len(y_positions) < 2 or y_positions[-1] <= y_positions[0]
	detected_edges: list[ShelfSurfaceEdge] = []
	position_idx = 0
	run_start_idx: int | None = None
	run_end_idx: int | None = None

	while position_idx < len(y_positions):
		y_mm = y_positions[position_idx]

		if is_unobserved is not None and is_unobserved(position_idx) is True:
			return detected_edges, position_idx

		if is_positive_depth(depth_values[position_idx]) is True:
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
			brim_height_mm=brim_height_mm,
			min_positive_run_length=min_positive_run_length,
		)

		if edge is None:
			run_start_idx = None
			run_end_idx = None
			position_idx += 1
			continue

		detected_edges.append(edge)

		if stop_after_first_edge is True:
			break

		skip_target_y_mm = y_mm - post_edge_skip_mm if descending is True else y_mm + post_edge_skip_mm

		while position_idx < len(y_positions) and (
			(y_positions[position_idx] > skip_target_y_mm)
			if descending is True
			else (y_positions[position_idx] < skip_target_y_mm)
		):
			position_idx += 1

		run_start_idx = None
		run_end_idx = None

	return detected_edges, None


def sample_scan_position_mm(sample: Any) -> float:
	"""Position a sample's depth belongs to; latency-corrected when the capture measured one."""
	depth_position_mm = getattr(sample, 'depth_position_mm', None)

	if depth_position_mm is not None:
		return float(depth_position_mm)

	return float(sample.position_mm)


class ShelfEdgeStopPredicate:
	"""Streaming twin of the confirmation rule in walk_shelf_edges_from_line.

	Returns True on the first sample that closes a positive run spanning at least
	`min_positive_run_length * grid_step_mm` of travel, so a moving pass can stop at the edge
	instead of flying the whole window. The post-pass detector bins the samples onto the scout
	grid and counts consecutive positive grid points; a span of that many grid steps always
	holds that many grid points (sample spacing below one step keeps them binned to the run's
	own samples), so the pass never stops on a run the detector would reject. An unconfirmed
	run still resets the run, and the confirmation itself is conservative: it can only delay
	the stop, never skip an edge the detector would have found.

	The tracked run belongs to one pass. Reset it when a pass is restarted and the samples it
	was following are discarded, otherwise the previous run's span confirms a short new one.
	"""

	def __init__(
		self,
		*,
		min_positive_run_length: int = SHELF_EDGE_CONFIRMATION_COUNT,
		grid_step_mm: float = SHELF_SCOUT_STEP_MM,
	) -> None:
		self._min_run_span_mm = float(min_positive_run_length) * float(grid_step_mm)
		self._run_start_mm: float | None = None
		self._run_end_mm: float | None = None

	def reset(self) -> None:
		self._run_start_mm = None
		self._run_end_mm = None

	def __call__(self, samples: list[Any]) -> bool:
		if len(samples) == 0:
			return False

		depth_mm = samples[-1].depth_mm

		if is_positive_depth(depth_mm) is True:
			position_mm = sample_scan_position_mm(samples[-1])

			if self._run_start_mm is None:
				self._run_start_mm = position_mm

			self._run_end_mm = position_mm
			return False

		if self._run_start_mm is None or self._run_end_mm is None:
			return False

		is_confirmed = abs(self._run_end_mm - self._run_start_mm) >= self._min_run_span_mm
		self.reset()

		return is_confirmed


def build_shelf_edge_stop_predicate(
	*,
	min_positive_run_length: int = SHELF_EDGE_CONFIRMATION_COUNT,
	grid_step_mm: float = SHELF_SCOUT_STEP_MM,
) -> ShelfEdgeStopPredicate:
	return ShelfEdgeStopPredicate(
		min_positive_run_length=min_positive_run_length,
		grid_step_mm=grid_step_mm,
	)

