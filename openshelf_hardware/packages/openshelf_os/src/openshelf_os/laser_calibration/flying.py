from __future__ import annotations

import asyncio
import bisect
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Callable, Optional

from .core import depth_to_label, print_status, raise_if_cancelled
from .shelf_scout import (
	SHELF_EDGE_CONFIRMATION_COUNT,
	ShelfSurfaceEdge,
	detect_shelf_edges_from_line,
)

FLYING_SCAN_BIN_NEAREST = "nearest"
FLYING_SCAN_BIN_MEDIAN = "median"
FLYING_SCAN_PROGRESS_EVERY = 25
FLYING_SCAN_PROGRESS_INTERVAL_S = 1.0
FLYING_SAMPLES_FILENAME = "flying_samples.jsonl"
# tool.gantry_wait caps a single line at GANTRY_WAIT_TIMEOUT (120s); refuse a feed that cannot
# finish a line inside it instead of letting the wait time out mid-pass.
FLYING_SCAN_MAX_LINE_SECONDS = 100.0


@dataclass
class FlyingScanSample:
	position_mm: float
	depth_mm: Optional[float]
	elapsed_s: float


@dataclass
class FlyingScanLine:
	"""One axis pass sampled while the axis moves, instead of one stop per grid point."""

	axis: str
	fixed_mm: float
	start_mm: float
	end_mm: float
	rpm: int
	samples: list[FlyingScanSample] = field(default_factory=list)
	duration_s: float = 0.0

	def median_sample_spacing_mm(self) -> Optional[float]:
		if len(self.samples) < 2:
			return None

		spacings = [
			abs(self.samples[index + 1].position_mm - self.samples[index].position_mm)
			for index in range(len(self.samples) - 1)
		]

		return float(median(spacings))

	def metrics_text(self) -> str:
		spacing_mm = self.median_sample_spacing_mm()
		feed_mm_s = 0.0

		if self.duration_s > 0.0:
			feed_mm_s = abs(float(self.end_mm) - float(self.start_mm)) / self.duration_s

		return (
			f"axis={self.axis} fixed_mm={self.fixed_mm:.2f} "
			f"start_mm={self.start_mm:.2f} end_mm={self.end_mm:.2f} rpm={self.rpm} "
			f"samples={len(self.samples)} duration_s={self.duration_s:.2f} "
			f"feed_mm_s={feed_mm_s:.2f} "
			f"median_spacing_mm={'none' if spacing_mm is None else f'{spacing_mm:.3f}'}"
		)


def build_nearest_position_index(positions: list[float]) -> Callable[[float], int]:
	"""Map a measured position onto its nearest grid index, for either grid direction."""
	ascending = positions[-1] >= positions[0]
	ordered_positions = positions if ascending is True else list(reversed(positions))
	position_count = len(ordered_positions)

	def didFindNearestIndex(position_mm: float) -> int:
		insert_at = bisect.bisect_left(ordered_positions, position_mm)

		if insert_at <= 0:
			chosen_index = 0
		elif insert_at >= position_count:
			chosen_index = position_count - 1
		else:
			gap_before_mm = abs(position_mm - ordered_positions[insert_at - 1])
			gap_after_mm = abs(ordered_positions[insert_at] - position_mm)
			chosen_index = insert_at - 1 if gap_before_mm <= gap_after_mm else insert_at

		return chosen_index if ascending is True else position_count - 1 - chosen_index

	return didFindNearestIndex


def bin_samples_to_positions(
	*,
	samples: list[FlyingScanSample],
	positions: list[float],
	max_distance_mm: Optional[float] = None,
	mode: str = FLYING_SCAN_BIN_NEAREST,
) -> list[Optional[float]]:
	"""Snap moving samples onto the uniform grid the stepped scan would have probed.

	`nearest` keeps the single sample closest to each grid position, which is the closest
	analogue of a stepped probe and the default for comparing a flying run against a stepped
	one. `median` summarizes every sample that falls in a grid cell, for noisy sensors.
	"""
	if len(positions) == 0:
		return []

	if mode not in (FLYING_SCAN_BIN_NEAREST, FLYING_SCAN_BIN_MEDIAN):
		raise ValueError(f"Unsupported flying scan bin mode: {mode}")

	if max_distance_mm is None:
		max_distance_mm = abs(positions[1] - positions[0]) if len(positions) > 1 else float("inf")

	find_index = build_nearest_position_index(positions)

	if mode == FLYING_SCAN_BIN_MEDIAN:
		buckets: list[list[float]] = [[] for _ in positions]

		for sample in samples:
			if sample.depth_mm is None:
				continue

			index = find_index(sample.position_mm)

			if abs(sample.position_mm - positions[index]) > max_distance_mm:
				continue

			buckets[index].append(float(sample.depth_mm))

		return [
			float(median(bucket)) if len(bucket) > 0 else None
			for bucket in buckets
		]

	values: list[Optional[float]] = [None for _ in positions]
	best_distance_mm: list[Optional[float]] = [None for _ in positions]

	for sample in samples:
		if sample.depth_mm is None:
			continue

		index = find_index(sample.position_mm)
		distance_mm = abs(sample.position_mm - positions[index])

		if distance_mm > max_distance_mm:
			continue

		if best_distance_mm[index] is not None and distance_mm >= best_distance_mm[index]:
			continue

		best_distance_mm[index] = distance_mm
		values[index] = float(sample.depth_mm)

	return values


def estimate_axis_feed_mm_s(motor: Any, rpm: int) -> Optional[float]:
	"""Rough mm/s for a commanded rpm, using the axis's mm-per-revolution when known."""
	params = getattr(motor, "_params", None)
	mpr = getattr(params, "mpr", None)

	if not isinstance(mpr, (int, float)) or float(mpr) <= 0.0:
		return None

	return float(rpm) * float(mpr) / 60.0


def write_flying_line_samples(
	*,
	run_dir: Optional[Path],
	label: str,
	line: FlyingScanLine,
) -> Optional[str]:
	"""Keep the raw moving samples so cadence, feed and edge sharpness can be reviewed."""
	if run_dir is None:
		return None

	output_path = Path(run_dir) / FLYING_SAMPLES_FILENAME
	output_path.parent.mkdir(parents=True, exist_ok=True)

	with output_path.open("a", encoding="utf-8") as output_file:
		for sample in line.samples:
			output_file.write(
				json.dumps(
					{
						"label": label,
						"axis": line.axis,
						"fixed_mm": line.fixed_mm,
						"start_mm": line.start_mm,
						"end_mm": line.end_mm,
						"rpm": line.rpm,
						"position_mm": sample.position_mm,
						"depth_mm": sample.depth_mm,
						"elapsed_s": sample.elapsed_s,
					}
				)
				+ "\n"
			)

	return str(output_path)


async def _stop_flying_axes(tool: Any, scan_motor: Any, hold_motor: Any) -> None:
	# The drive can already be triggered while the motor has not registered its pending move yet,
	# so a plain stop goes out first; gantry_wait then clears the pending state it may know about.
	# Leaving pending motion behind would make the next move look like this one's completion.
	for motor in (scan_motor, hold_motor):
		try:
			await motor.stop()
		except Exception:
			continue

	gantry_wait = getattr(tool, "gantry_wait", None)

	if gantry_wait is None:
		return

	await asyncio.gather(
		gantry_wait(should_abort=lambda: True),
		return_exceptions=True,
	)


async def scan_axis_flying(
	*,
	tool: Any,
	axis: str,
	start_mm: float,
	end_mm: float,
	fixed_mm: float,
	rpm: int,
	progress_label: str,
	progress_positions: Optional[list[float]] = None,
	progress_every: int = FLYING_SCAN_PROGRESS_EVERY,
	progress_printer: Optional[Callable[[str], None]] = None,
	should_abort: Optional[Callable[[], bool]] = None,
) -> FlyingScanLine:
	if axis not in ("x", "y"):
		raise ValueError("flying scan axis must be 'x' or 'y'")

	if rpm <= 0:
		raise ValueError("flying scan rpm must be greater than zero")

	scan_motor = getattr(tool, "_mx" if axis == "x" else "_my", None)
	hold_motor = getattr(tool, "_my" if axis == "x" else "_mx", None)

	if scan_motor is None or hold_motor is None:
		raise ValueError(f"Gantry motors are required for a flying {axis} scan")

	estimated_feed_mm_s = estimate_axis_feed_mm_s(scan_motor, rpm)

	if estimated_feed_mm_s is not None:
		estimated_duration_s = abs(float(end_mm) - float(start_mm)) / estimated_feed_mm_s

		if estimated_duration_s > FLYING_SCAN_MAX_LINE_SECONDS:
			raise ValueError(
				f"flying {axis} scan of {abs(float(end_mm) - float(start_mm)):.0f}mm at {rpm}rpm "
				f"would take ~{estimated_duration_s:.0f}s, and a line is capped at "
				f"{FLYING_SCAN_MAX_LINE_SECONDS:.0f}s; raise the rpm or shorten the bounds"
			)

	printer = progress_printer if progress_printer is not None else print_status
	raise_if_cancelled(should_abort)

	await hold_motor.move_to_position_no_wait(fixed_mm)
	await scan_motor.move_to_position_no_wait(start_mm)
	await tool.gantry_wait(should_abort=should_abort)
	raise_if_cancelled(should_abort)

	find_progress_index: Optional[Callable[[float], int]] = None

	if progress_positions is not None and len(progress_positions) > 0:
		find_progress_index = build_nearest_position_index(progress_positions)

	samples: list[FlyingScanSample] = []
	covered_indices: set[int] = set()
	started_at = time.monotonic()
	last_print_at = started_at
	last_covered_count = 0

	# One stationary sample at the start before the move is commanded. The trigger dwell eats
	# the first millimetres of travel, so without it the first grid position would only be
	# covered by whichever moving sample happens to land nearest.
	start_depth_mm = await tool.get_laser_distance()
	start_position_mm = float(await scan_motor.get_position())
	samples.append(
		FlyingScanSample(
			position_mm=start_position_mm,
			depth_mm=None if start_depth_mm is None else float(start_depth_mm),
			elapsed_s=time.monotonic() - started_at,
		)
	)

	if find_progress_index is not None:
		covered_indices.add(find_progress_index(start_position_mm))

	move_task: Optional[asyncio.Task[None]] = None

	try:
		await scan_motor.move_to_position_no_wait(end_mm, rpm=rpm)
		move_task = asyncio.create_task(tool.gantry_wait(should_abort=should_abort))

		while True:
			raise_if_cancelled(should_abort)
			depth_mm = await tool.get_laser_distance()
			position_mm = float(await scan_motor.get_position())
			sampled_at = time.monotonic()
			samples.append(
				FlyingScanSample(
					position_mm=position_mm,
					depth_mm=None if depth_mm is None else float(depth_mm),
					elapsed_s=sampled_at - started_at,
				)
			)

			if find_progress_index is not None:
				covered_indices.add(find_progress_index(position_mm))

			is_due = (sampled_at - last_print_at) >= FLYING_SCAN_PROGRESS_INTERVAL_S

			if (
				find_progress_index is not None
				and len(covered_indices) - last_covered_count >= progress_every
			):
				is_due = True

			if is_due is True:
				last_print_at = sampled_at
				last_covered_count = len(covered_indices)

				if find_progress_index is not None:
					x_mm, y_mm = (
						(position_mm, fixed_mm) if axis == "x" else (fixed_mm, position_mm)
					)
					printer(
						f"{progress_label}={len(covered_indices)}/{len(progress_positions)} "
						f"x={x_mm:.2f} y={y_mm:.2f} depth={depth_to_label(depth_mm)}"
					)
				else:
					printer(
						f"flying_line={progress_label} samples={len(samples)} "
						f"position={position_mm:.2f} depth={depth_to_label(depth_mm)}"
					)

			if move_task.done() is True:
				break

		await move_task
	except BaseException:
		if move_task is not None:
			move_task.cancel()
			await asyncio.gather(move_task, return_exceptions=True)

		await _stop_flying_axes(tool, scan_motor, hold_motor)
		raise

	line = FlyingScanLine(
		axis=axis,
		fixed_mm=fixed_mm,
		start_mm=start_mm,
		end_mm=end_mm,
		rpm=int(rpm),
		samples=samples,
		duration_s=time.monotonic() - started_at,
	)
	printer(f"flying_line={progress_label} {line.metrics_text()}")

	return line


async def scan_line_positions_flying(
	tool: Any,
	*,
	axis: str,
	positions: list[float],
	fixed_mm: float,
	rpm: int,
	progress_label: str,
	should_abort: Optional[Callable[[], bool]] = None,
	bin_mode: str = FLYING_SCAN_BIN_NEAREST,
	progress_every: int = FLYING_SCAN_PROGRESS_EVERY,
	progress_printer: Optional[Callable[[str], None]] = None,
) -> tuple[list[Optional[float]], FlyingScanLine]:
	if len(positions) == 0:
		return [], FlyingScanLine(
			axis=axis,
			fixed_mm=fixed_mm,
			start_mm=0.0,
			end_mm=0.0,
			rpm=int(rpm),
		)

	line = await scan_axis_flying(
		tool=tool,
		axis=axis,
		start_mm=float(positions[0]),
		end_mm=float(positions[-1]),
		fixed_mm=fixed_mm,
		rpm=rpm,
		progress_label=progress_label,
		progress_positions=positions,
		progress_every=progress_every,
		progress_printer=progress_printer,
		should_abort=should_abort,
	)
	max_distance_mm = abs(positions[1] - positions[0]) if len(positions) > 1 else None
	depth_values = bin_samples_to_positions(
		samples=line.samples,
		positions=positions,
		max_distance_mm=max_distance_mm,
		mode=bin_mode,
	)

	return depth_values, line


async def scan_shelf_scout_line_flying(
	tool: Any,
	*,
	x_mm: float,
	y_positions: list[float],
	rpm: int,
	progress_label: str,
	should_abort: Optional[Callable[[], bool]] = None,
	post_edge_skip_mm: float = 0.0,
	brim_height_mm: Optional[float] = None,
	min_positive_run_length: int = SHELF_EDGE_CONFIRMATION_COUNT,
	bin_mode: str = FLYING_SCAN_BIN_NEAREST,
	progress_every: int = FLYING_SCAN_PROGRESS_EVERY,
	progress_printer: Optional[Callable[[str], None]] = None,
) -> tuple[list[Optional[float]], list[ShelfSurfaceEdge], FlyingScanLine]:
	depth_values, line = await scan_line_positions_flying(
		tool,
		axis="y",
		positions=y_positions,
		fixed_mm=x_mm,
		rpm=rpm,
		progress_label=progress_label,
		should_abort=should_abort,
		bin_mode=bin_mode,
		progress_every=progress_every,
		progress_printer=progress_printer,
	)
	brim_height_kwargs: dict[str, float] = {}

	if brim_height_mm is not None:
		brim_height_kwargs["brim_height_mm"] = float(brim_height_mm)

	edges = detect_shelf_edges_from_line(
		x_mm=x_mm,
		y_positions=y_positions,
		depth_values=depth_values,
		post_edge_skip_mm=post_edge_skip_mm,
		min_positive_run_length=min_positive_run_length,
		**brim_height_kwargs,
	)

	return depth_values, edges, line
