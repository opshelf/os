from __future__ import annotations

import asyncio
import bisect
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable, Optional

from .core import (
	COARSE_SCAN_STYLE_FLYING,
	LineGapSegment,
	depth_to_label,
	get_fine_scan_feed_mm_s,
	get_fine_scan_style,
	get_flying_capture_latency_s,
	print_status,
	raise_if_cancelled,
)
from .defaults import FLYING_CAPTURE_LATENCY_MS
from .motion import sample_laser_depth
from .shelf_scout import (
	SHELF_EDGE_CONFIRMATION_COUNT,
	SampleDepth,
	ShelfSurfaceEdge,
	walk_shelf_edges_from_line,
)

FLYING_SCAN_BIN_NEAREST = "nearest"
FLYING_SCAN_BIN_MEDIAN = "median"
FLYING_SCAN_PROGRESS_EVERY = 25
FLYING_SCAN_PROGRESS_INTERVAL_S = 1.0
FLYING_SAMPLES_FILENAME = "flying_samples.jsonl"
# tool.gantry_wait caps a single line at GANTRY_WAIT_TIMEOUT (120s); refuse a feed that cannot
# finish a line inside it instead of letting the wait time out mid-pass.
FLYING_SCAN_MAX_LINE_SECONDS = 100.0
# The drive stops reporting progress when it has finished or faulted; the sampler breaks on that
# and lets gantry_wait surface the reason.
FLYING_SCAN_STALL_S = 0.5
FLYING_SCAN_PROGRESS_EPS_MM = 0.02
# Velocity for the depth-to-position correction is a median over the trailing deltas so a single
# jittered timestamp cannot move a sample.
FLYING_SCAN_VELOCITY_WINDOW = 8
# The latency correction assumes every laser read takes about as long as the last ones. A
# retried Modbus read can take seconds, and the encoder read that follows it lands far past
# where the beam was, so a moving read slower than this multiple of the trailing median span is
# dropped. The cap never falls below the floor, so scheduler jitter on a fast bus keeps its samples,
# and it is only applied once the window holds enough spans to give a meaningful median.
FLYING_SCAN_SLOW_READ_FACTOR = 3.0
FLYING_SCAN_SLOW_READ_FLOOR_S = 0.025
FLYING_SCAN_SLOW_READ_WINDOW = 16
FLYING_SCAN_SLOW_READ_MIN_SPANS = 4
# The correction shifts every depth backwards by feed * capture_latency_s, so a pass that stops at
# end_mm never captures the raw samples the final grid positions would bin from. The pass flies the
# latency shift plus this margin past the grid; the margin covers the half-step binning tolerance of
# the 1mm fine and 4mm coarse grids.
FLYING_SCAN_LEAD_OUT_MARGIN_MM = 1.0
# A triggered axis that has not moved at all within this window is not going to complete the
# line; break and let gantry_wait classify the drive state.
FLYING_SCAN_START_STALL_S = 2.0
# After a line stops itself, the axis is commanded to stop and the sampler waits this long for
# the drive to settle before the next move is issued.
FLYING_SCAN_EARLY_STOP_SETTLE_S = 2.0
FLYING_SCAN_EARLY_STOP_POLL_S = 0.05
# A pass interrupted by a safety guard is restarted once the guard clears; the sampler polls the
# interlock at this interval so session cancellation still lands while it waits.
FLYING_SCAN_SAFETY_POLL_S = 0.1
# Tolerance when matching an interpolated crossing to the grid interval an edge sits in.
FLYING_EDGE_INTERVAL_EPS_MM = 0.01


@dataclass
class FlyingScanSample:
	"""One depth read, its raw encoder position, and the corrected position of the depth."""

	position_mm: float
	depth_mm: Optional[float]
	elapsed_s: float
	# Beam position at the depth's effective time: the encoder read lags the laser reading by a
	# fixed latency, so a moving pass assigns each depth to `position_mm - velocity * latency`.
	depth_position_mm: Optional[float] = None
	velocity_mm_s: Optional[float] = None


def sample_depth_position_mm(sample: FlyingScanSample) -> float:
	"""Position the depth belongs to; corrected when the capture measured a velocity."""
	if sample.depth_position_mm is not None:
		return float(sample.depth_position_mm)

	return float(sample.position_mm)


def estimate_sample_velocity_mm_s(
	samples: list[FlyingScanSample],
	*,
	window: int = FLYING_SCAN_VELOCITY_WINDOW,
) -> Optional[float]:
	"""Signed trailing velocity from consecutive samples, robust to one jittered read."""
	if len(samples) < 2:
		return None

	recent_samples = samples[-window:]
	velocities: list[float] = []

	for index in range(1, len(recent_samples)):
		elapsed_delta_s = recent_samples[index].elapsed_s - recent_samples[index - 1].elapsed_s

		if elapsed_delta_s <= 0.0:
			continue

		position_delta_mm = recent_samples[index].position_mm - recent_samples[index - 1].position_mm
		velocities.append(position_delta_mm / elapsed_delta_s)

	if len(velocities) == 0:
		return None

	return float(median(velocities))


def resolve_slow_read_cap_s(read_spans_s: "deque[float] | list[float]") -> Optional[float]:
	"""Longest laser read whose depth can still be placed by the fixed latency correction."""
	if len(read_spans_s) < FLYING_SCAN_SLOW_READ_MIN_SPANS:
		return None

	return max(
		FLYING_SCAN_SLOW_READ_FACTOR * float(median(read_spans_s)),
		FLYING_SCAN_SLOW_READ_FLOOR_S,
	)


@dataclass
class FlyingDepthCrossing:
	"""Where a moving depth profile crosses a threshold, interpolated between samples."""

	position_mm: float
	is_rising: bool
	from_depth_mm: float
	to_depth_mm: float


def find_depth_threshold_crossings(
	*,
	samples: list[FlyingScanSample],
	threshold_mm: float,
) -> list[FlyingDepthCrossing]:
	"""Interpolate every threshold crossing in acquisition order.

	Fine-scan edges are a threshold crossing between a wall reading and a hole reading; the
	samples either side of it bracket the edge to well under one sample spacing, which is the
	resolution the stepped scan spends a settle and a move per point to reach.
	"""
	crossings: list[FlyingDepthCrossing] = []

	for index in range(1, len(samples)):
		previous = samples[index - 1]
		current = samples[index]

		if previous.depth_mm is None or current.depth_mm is None:
			continue

		from_depth_mm = float(previous.depth_mm)
		to_depth_mm = float(current.depth_mm)
		is_crossing = (from_depth_mm <= threshold_mm < to_depth_mm) or (
			from_depth_mm >= threshold_mm > to_depth_mm
		)

		if is_crossing is False:
			continue

		from_position_mm = sample_depth_position_mm(previous)
		to_position_mm = sample_depth_position_mm(current)
		depth_span_mm = to_depth_mm - from_depth_mm

		if depth_span_mm == 0.0:
			crossing_position_mm = to_position_mm
		else:
			fraction = (threshold_mm - from_depth_mm) / depth_span_mm
			crossing_position_mm = from_position_mm + (to_position_mm - from_position_mm) * fraction

		crossings.append(
			FlyingDepthCrossing(
				position_mm=crossing_position_mm,
				is_rising=to_depth_mm > from_depth_mm,
				from_depth_mm=from_depth_mm,
				to_depth_mm=to_depth_mm,
			)
		)

	return crossings


def _interpolate_edge_position(
	*,
	positions: list[float],
	depth_values: list[Optional[float]],
	samples: list[FlyingScanSample],
	wall_idx: int,
	hole_idx: int,
	fallback_mm: float,
) -> float:
	if wall_idx < 0 or hole_idx < 0:
		return fallback_mm

	if wall_idx >= len(positions) or hole_idx >= len(positions):
		return fallback_mm

	wall_depth_mm = depth_values[wall_idx]
	hole_depth_mm = depth_values[hole_idx]

	if wall_depth_mm is None or hole_depth_mm is None:
		return fallback_mm

	from_position_mm = float(positions[wall_idx])
	to_position_mm = float(positions[hole_idx])
	threshold_mm = (float(wall_depth_mm) + float(hole_depth_mm)) / 2.0
	lower_mm = min(from_position_mm, to_position_mm) - FLYING_EDGE_INTERVAL_EPS_MM
	upper_mm = max(from_position_mm, to_position_mm) + FLYING_EDGE_INTERVAL_EPS_MM
	crossings = [
		crossing.position_mm
		for crossing in find_depth_threshold_crossings(samples=samples, threshold_mm=threshold_mm)
		if lower_mm <= crossing.position_mm <= upper_mm
	]

	if len(crossings) == 0:
		return fallback_mm

	interval_midpoint_mm = (from_position_mm + to_position_mm) / 2.0

	return min(crossings, key=lambda position_mm: abs(position_mm - interval_midpoint_mm))


def refine_segment_edges_from_samples(
	*,
	segment: LineGapSegment,
	positions: list[float],
	depth_values: list[Optional[float]],
	samples: list[FlyingScanSample],
) -> LineGapSegment:
	"""Replace a grid-quantized gap segment's edges with interpolated crossings.

	The decision walk still runs on the binned grid, so the segment's indices and whether it
	was accepted are unchanged; only the millimetre edges improve, and those are what the cell
	centre is built from. An edge with no crossing in its interval (an inferred far edge) keeps
	the grid estimate.
	"""
	start_mm = _interpolate_edge_position(
		positions=positions,
		depth_values=depth_values,
		samples=samples,
		wall_idx=int(segment.start_idx) - 1,
		hole_idx=int(segment.start_idx),
		fallback_mm=float(segment.start_mm),
	)
	end_mm = _interpolate_edge_position(
		positions=positions,
		depth_values=depth_values,
		samples=samples,
		wall_idx=int(segment.end_idx) + 1,
		hole_idx=int(segment.end_idx),
		fallback_mm=float(segment.end_mm),
	)

	if end_mm <= start_mm:
		return segment

	return LineGapSegment(
		start_idx=segment.start_idx,
		end_idx=segment.end_idx,
		start_mm=start_mm,
		end_mm=end_mm,
		width_mm=float(end_mm - start_mm),
		center_mm=float((start_mm + end_mm) / 2.0),
	)


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
	# Moving reads discarded because the laser transaction ran past the slow-read cap.
	dropped_slow_reads: int = 0
	# Latency each depth_position_mm was corrected with, so a replay can undo or redo it.
	capture_latency_s: Optional[float] = None

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
		# Both ends of a pass can disagree with the commanded range: the start may have been
		# clamped to the axis limit and an early stop never reaches end_mm. Measure the distance
		# actually travelled between the first and last samples instead.
		travel_mm = abs(float(self.end_mm) - float(self.start_mm))

		if len(self.samples) > 1:
			first_position_mm = sample_depth_position_mm(self.samples[0])
			last_position_mm = sample_depth_position_mm(self.samples[-1])
			travel_mm = abs(last_position_mm - first_position_mm)

		if self.duration_s > 0.0:
			feed_mm_s = travel_mm / self.duration_s

		return (
			f"axis={self.axis} fixed_mm={self.fixed_mm:.2f} "
			f"start_mm={self.start_mm:.2f} end_mm={self.end_mm:.2f} rpm={self.rpm} "
			f"samples={len(self.samples)} duration_s={self.duration_s:.2f} "
			f"feed_mm_s={feed_mm_s:.2f} "
			f"median_spacing_mm={'none' if spacing_mm is None else f'{spacing_mm:.3f}'} "
			f"dropped_slow_reads={self.dropped_slow_reads}"
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

			sample_position_mm = sample_depth_position_mm(sample)
			index = find_index(sample_position_mm)

			if abs(sample_position_mm - positions[index]) > max_distance_mm:
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

		sample_position_mm = sample_depth_position_mm(sample)
		index = find_index(sample_position_mm)
		distance_mm = abs(sample_position_mm - positions[index])

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


def resolve_axis_rpm_for_feed(motor: Any, feed_mm_s: float) -> Optional[int]:
	"""Commanded rpm that moves this axis at roughly the requested mm/s."""
	params = getattr(motor, "_params", None)
	mpr = getattr(params, "mpr", None)

	if not isinstance(mpr, (int, float)) or float(mpr) <= 0.0 or feed_mm_s <= 0.0:
		return None

	return max(1, int(round(float(feed_mm_s) * 60.0 / float(mpr))))


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
						"capture_latency_s": line.capture_latency_s,
						"position_mm": sample.position_mm,
						"depth_position_mm": sample.depth_position_mm,
						"velocity_mm_s": sample.velocity_mm_s,
						"depth_mm": sample.depth_mm,
						"elapsed_s": sample.elapsed_s,
					}
				)
				+ "\n"
			)

	return str(output_path)


async def _stop_axis_early(scan_motor: Any) -> None:
	"""Stop a pass that ended itself, without treating the interrupted target as a fault.

	A line stopped by its own predicate is not a failed move: the drive has to stop, and the
	pending target must be cleared so a later gantry_wait does not wait for the original end or
	compare the stopped position against it.
	"""
	stop_and_clear = getattr(scan_motor, "_stop_and_clear_motion", None)

	if stop_and_clear is not None:
		await stop_and_clear()
	else:
		await scan_motor.stop()
		clear_pending = getattr(scan_motor, "_clear_pending_motion", None)

		if clear_pending is not None:
			clear_pending()

	last_position_mm: Optional[float] = None
	deadline = time.monotonic() + FLYING_SCAN_EARLY_STOP_SETTLE_S

	while time.monotonic() < deadline:
		position_mm = float(await scan_motor.get_position())

		if (
			last_position_mm is not None
			and abs(position_mm - last_position_mm) <= FLYING_SCAN_PROGRESS_EPS_MM
		):
			return

		last_position_mm = position_mm
		await asyncio.sleep(FLYING_SCAN_EARLY_STOP_POLL_S)


class FlyingScanSafetyInterrupted(Exception):
	"""A safety guard engaged while a pass was sampling, so its samples were discarded."""


def _is_safety_blocked(*motors: Any) -> bool:
	# Motor.wait_for_move only pauses for the guard while something waits on the move, and a
	# flying pass defers that wait until acquisition ends, so the sampler checks the interlock.
	for motor in motors:
		safety = getattr(motor, "_safety", None)

		if safety is not None and getattr(safety, "blocked", False) is True:
			return True

	return False


async def _wait_for_safety_clear(
	*motors: Any,
	should_abort: Optional[Callable[[], bool]] = None,
) -> None:
	while _is_safety_blocked(*motors) is True:
		raise_if_cancelled(should_abort)
		await asyncio.sleep(FLYING_SCAN_SAFETY_POLL_S)

	raise_if_cancelled(should_abort)


async def _restore_axis_velocity(scan_motor: Any) -> None:
	"""Write the configured feed back after a pass commanded its own rpm.

	Motor writes a move's rpm into the drive's persistent Velocity register, and later moves with
	rpm=None reuse whatever the register holds, so a pass left unrestored slows every move after it.
	"""
	set_param = getattr(scan_motor, "set_param", None)
	configured_rpm = getattr(getattr(scan_motor, "_params", None), "rpm", None)

	if set_param is None or not isinstance(configured_rpm, (int, float)):
		return

	await set_param("Velocity", int(configured_rpm))


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
	capture_latency_s: Optional[float] = None,
	stop_when: Optional[Callable[[list[FlyingScanSample]], bool]] = None,
) -> FlyingScanLine:
	"""Fly one line, restarting it from start_mm whenever a safety guard interrupts the pass.

	A stepped move pauses at the guard and resumes toward the same target, but a flying pass
	cannot splice samples taken across a stop into one latency-corrected line, so the interrupted
	pass is discarded and flown again after the guard clears.
	"""
	printer = progress_printer if progress_printer is not None else print_status

	while True:
		try:
			return await _scan_axis_flying_pass(
				tool=tool,
				axis=axis,
				start_mm=start_mm,
				end_mm=end_mm,
				fixed_mm=fixed_mm,
				rpm=rpm,
				progress_label=progress_label,
				progress_positions=progress_positions,
				progress_every=progress_every,
				progress_printer=printer,
				should_abort=should_abort,
				capture_latency_s=capture_latency_s,
				stop_when=stop_when,
			)
		except FlyingScanSafetyInterrupted:
			printer(f"flying_line={progress_label} safety_guard=stopped restart=after_clear")
			await _wait_for_safety_clear(
				getattr(tool, "_mx", None),
				getattr(tool, "_my", None),
				should_abort=should_abort,
			)


async def _scan_axis_flying_pass(
	*,
	tool: Any,
	axis: str,
	start_mm: float,
	end_mm: float,
	fixed_mm: float,
	rpm: int,
	progress_label: str,
	progress_positions: Optional[list[float]],
	progress_every: int,
	progress_printer: Callable[[str], None],
	should_abort: Optional[Callable[[], bool]],
	capture_latency_s: Optional[float],
	stop_when: Optional[Callable[[list[FlyingScanSample]], bool]],
) -> FlyingScanLine:
	if axis not in ("x", "y"):
		raise ValueError("flying scan axis must be 'x' or 'y'")

	if rpm <= 0:
		raise ValueError("flying scan rpm must be greater than zero")

	if capture_latency_s is None:
		capture_latency_s = FLYING_CAPTURE_LATENCY_MS / 1000.0

	# A stateful stop predicate carries the run it was tracking. Every pass starts with a new
	# sample list, so reset the predicate too, or an interrupted pass's run confirms the
	# restarted one and the line stops early.
	didResetStopPredicate = getattr(stop_when, "reset", None)

	if didResetStopPredicate is not None:
		didResetStopPredicate()

	scan_motor = getattr(tool, "_mx" if axis == "x" else "_my", None)
	hold_motor = getattr(tool, "_my" if axis == "x" else "_mx", None)

	if scan_motor is None or hold_motor is None:
		raise ValueError(f"Gantry motors are required for a flying {axis} scan")

	estimated_feed_mm_s = estimate_axis_feed_mm_s(scan_motor, rpm)
	direction = 1.0 if float(end_mm) >= float(start_mm) else -1.0

	lead_out_mm = 0.0

	if capture_latency_s > 0.0 and estimated_feed_mm_s is not None:
		lead_out_mm = estimated_feed_mm_s * capture_latency_s + FLYING_SCAN_LEAD_OUT_MARGIN_MM

	motion_end_mm = float(end_mm) + direction * lead_out_mm
	scan_params = getattr(scan_motor, "_params", None)
	lower_lim_mm = getattr(scan_params, "lower_lim", None)
	upper_lim_mm = getattr(scan_params, "lim", None)

	if isinstance(lower_lim_mm, (int, float)) and isinstance(upper_lim_mm, (int, float)):
		motion_end_mm = min(max(motion_end_mm, float(lower_lim_mm)), float(upper_lim_mm))

	if estimated_feed_mm_s is not None:
		estimated_duration_s = abs(motion_end_mm - float(start_mm)) / estimated_feed_mm_s

		if estimated_duration_s > FLYING_SCAN_MAX_LINE_SECONDS:
			raise ValueError(
				f"flying {axis} scan of {abs(float(end_mm) - float(start_mm)):.0f}mm at {rpm}rpm "
				f"would take ~{estimated_duration_s:.0f}s, and a line is capped at "
				f"{FLYING_SCAN_MAX_LINE_SECONDS:.0f}s; raise the rpm or shorten the bounds"
			)

	printer = progress_printer
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
	# covered by whichever moving sample happens to land nearest. The far end is covered by the
	# lead-out instead: the latency correction shifts every moving sample backwards, so flying
	# lead_out_mm past end_mm keeps raw samples under the final grid positions rather than
	# leaving their bins empty.
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

	has_moved = False
	last_position_mm = start_position_mm
	last_progress_at = started_at
	stopped_early = False
	# Span of each laser transaction alone; the history includes dropped reads so a bus that
	# slows down for good raises the cap instead of discarding the rest of the line.
	read_spans_s: deque[float] = deque(maxlen=FLYING_SCAN_SLOW_READ_WINDOW)
	dropped_slow_reads = 0

	try:
		did_start = await scan_motor.move_to_position_no_wait(motion_end_mm, rpm=rpm)

		while did_start is True:
			raise_if_cancelled(should_abort)

			if _is_safety_blocked(scan_motor, hold_motor) is True:
				raise FlyingScanSafetyInterrupted()

			read_started_at = time.monotonic()
			depth_mm = await tool.get_laser_distance()
			read_span_s = time.monotonic() - read_started_at
			position_mm = float(await scan_motor.get_position())
			sampled_at = time.monotonic()
			slow_read_cap_s = resolve_slow_read_cap_s(read_spans_s)
			read_spans_s.append(read_span_s)
			# A slow read's depth belongs well before this encoder position, and no fixed
			# correction can say where. Keep the read for the motion checks below but leave it
			# out of the samples, so it neither bins nor feeds the velocity or stop predicate.
			is_slow_read = slow_read_cap_s is not None and read_span_s > slow_read_cap_s

			if is_slow_read is True:
				dropped_slow_reads += 1
			else:
				sample = FlyingScanSample(
					position_mm=position_mm,
					depth_mm=None if depth_mm is None else float(depth_mm),
					elapsed_s=sampled_at - started_at,
				)
				samples.append(sample)
				velocity_mm_s = estimate_sample_velocity_mm_s(samples)

				if velocity_mm_s is not None:
					sample.velocity_mm_s = velocity_mm_s
					sample.depth_position_mm = position_mm - velocity_mm_s * capture_latency_s

				if find_progress_index is not None:
					covered_indices.add(find_progress_index(sample_depth_position_mm(sample)))

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

			if is_slow_read is False and stop_when is not None and stop_when(samples) is True:
				stopped_early = True
				break

			if direction * (position_mm - motion_end_mm) >= 0.0:
				break

			# Move the anchor only once motion clears the epsilon: at low feeds consecutive
			# reads differ by less than it, and re-anchoring every read would never register
			# the axis as moving.
			if abs(position_mm - last_position_mm) > FLYING_SCAN_PROGRESS_EPS_MM:
				has_moved = True
				last_progress_at = sampled_at
				last_position_mm = position_mm

			if has_moved is True and (sampled_at - last_progress_at) > FLYING_SCAN_STALL_S:
				break

			if has_moved is False and (sampled_at - started_at) > FLYING_SCAN_START_STALL_S:
				break

			if (sampled_at - started_at) > FLYING_SCAN_MAX_LINE_SECONDS:
				raise TimeoutError(
					f"flying {axis} line did not reach {motion_end_mm:.2f}mm within "
					f"{FLYING_SCAN_MAX_LINE_SECONDS:.0f}s"
				)

		if stopped_early is True:
			await _stop_axis_early(scan_motor)
		else:
			await tool.gantry_wait(should_abort=should_abort)
			# Mirror the stationary start sample with one at the settled end: an axis limit can
			# clamp the lead-out away, and without this read the latency-corrected cloud stops
			# feed*latency short of the final grid position and leaves its bin empty. The axis
			# is at rest, so the depth sits at the measured position with no latency shift.
			end_depth_mm = await tool.get_laser_distance()
			end_position_mm = float(await scan_motor.get_position())
			samples.append(
				FlyingScanSample(
					position_mm=end_position_mm,
					depth_mm=None if end_depth_mm is None else float(end_depth_mm),
					elapsed_s=time.monotonic() - started_at,
					depth_position_mm=end_position_mm,
					velocity_mm_s=0.0,
				)
			)
	except BaseException:
		await _stop_flying_axes(tool, scan_motor, hold_motor)

		try:
			await _restore_axis_velocity(scan_motor)
		except Exception:
			pass

		raise

	await _restore_axis_velocity(scan_motor)

	line = FlyingScanLine(
		axis=axis,
		fixed_mm=fixed_mm,
		start_mm=start_mm,
		end_mm=end_mm,
		rpm=int(rpm),
		samples=samples,
		duration_s=time.monotonic() - started_at,
		dropped_slow_reads=dropped_slow_reads,
		capture_latency_s=capture_latency_s,
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
	capture_latency_s: Optional[float] = None,
	stop_when: Optional[Callable[[list[FlyingScanSample]], bool]] = None,
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
		capture_latency_s=capture_latency_s,
		stop_when=stop_when,
	)
	max_distance_mm = abs(positions[1] - positions[0]) if len(positions) > 1 else None
	depth_values = bin_samples_to_positions(
		samples=line.samples,
		positions=positions,
		max_distance_mm=max_distance_mm,
		mode=bin_mode,
	)

	return depth_values, line


async def prefetch_line_depth_values(
	tool: Any,
	*,
	scan_config: Any,
	axis: str,
	positions: list[float],
	fixed_mm: float,
	progress_label: str,
	should_abort: Optional[Callable[[], bool]] = None,
) -> Optional[tuple[list[Optional[float]], FlyingScanLine]]:
	"""Fly a line when the fine scan is configured for it; None means the caller must step it.

	The caller replays its own decision walk over the returned grid values, so its adaptive
	order, progress output, retries and persisted grid stay identical to the stepped scan.
	"""
	if get_fine_scan_style(scan_config) != COARSE_SCAN_STYLE_FLYING:
		return None

	scan_motor = getattr(tool, "_mx" if axis == "x" else "_my", None)

	if scan_motor is None:
		return None

	rpm = resolve_axis_rpm_for_feed(scan_motor, get_fine_scan_feed_mm_s(scan_config))

	if rpm is None:
		return None

	depth_values, line = await scan_line_positions_flying(
		tool,
		axis=axis,
		positions=positions,
		fixed_mm=fixed_mm,
		rpm=rpm,
		progress_label=progress_label,
		should_abort=should_abort,
		capture_latency_s=get_flying_capture_latency_s(scan_config),
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
	capture_latency_s: Optional[float] = None,
	stop_after_first_edge: bool = False,
	stop_when: Optional[Callable[[list[FlyingScanSample]], bool]] = None,
	sample_depth: SampleDepth = sample_laser_depth,
	settle_ms: int = 0,
	samples_per_point: int = 1,
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
		capture_latency_s=capture_latency_s,
		stop_when=stop_when,
	)
	brim_height_kwargs: dict[str, float] = {}

	if brim_height_mm is not None:
		brim_height_kwargs["brim_height_mm"] = float(brim_height_mm)

	printer = progress_printer if progress_printer is not None else print_status

	async def didResampleDepth(y_mm: float) -> Optional[float]:
		raise_if_cancelled(should_abort)
		await tool._mx.move_to_position_no_wait(x_mm)
		await tool._my.move_to_position_no_wait(y_mm)
		await tool.gantry_wait(should_abort=should_abort)
		raise_if_cancelled(should_abort)
		depth_mm = await sample_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		printer(
			f"{progress_label}=resampled x={x_mm:.2f} y={y_mm:.2f} "
			f"depth={depth_to_label(depth_mm)}"
		)

		return depth_mm

	edges = await resolve_shelf_edges_from_line(
		x_mm=x_mm,
		y_positions=y_positions,
		depth_values=depth_values,
		resample_depth=didResampleDepth,
		post_edge_skip_mm=post_edge_skip_mm,
		min_positive_run_length=min_positive_run_length,
		stop_after_first_edge=stop_after_first_edge,
		**brim_height_kwargs,
	)

	return depth_values, edges, line


async def resolve_shelf_edges_from_line(
	*,
	x_mm: float,
	y_positions: list[float],
	depth_values: list[Optional[float]],
	resample_depth: Callable[[float], Awaitable[Optional[float]]],
	**walk_kwargs: Any,
) -> list[ShelfSurfaceEdge]:
	"""Detect shelf edges on a binned flying line, probing the grid bins the pass left empty.

	An empty bin holds no measurement, and the detector would otherwise read it as a gap that
	closes the positive run, inventing an edge whose post-edge skip can hide a real shelf. Only
	bins the stepped walk would read are probed, so skipped positions and the unflown tail of an
	early-stopped pass stay empty. Probed depths are written back into `depth_values`; a probe
	that still reads nothing counts as a measured non-positive depth, as in the stepped scan.
	"""
	resampled_indices: set[int] = set()

	def isUnobserved(position_idx: int) -> bool:
		return depth_values[position_idx] is None and position_idx not in resampled_indices

	while True:
		edges, unobserved_idx = walk_shelf_edges_from_line(
			x_mm=x_mm,
			y_positions=y_positions,
			depth_values=depth_values,
			is_unobserved=isUnobserved,
			**walk_kwargs,
		)

		if unobserved_idx is None:
			return edges

		depth_values[unobserved_idx] = await resample_depth(y_positions[unobserved_idx])
		resampled_indices.add(unobserved_idx)
