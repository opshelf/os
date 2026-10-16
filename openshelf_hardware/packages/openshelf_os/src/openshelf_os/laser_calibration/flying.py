from __future__ import annotations

import asyncio
import bisect
import json
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Awaitable, Callable, Iterator, Optional

from hardware.exceptions import CommunicationError, MotorError

from .core import (
	COARSE_SCAN_STYLE_FLYING,
	LineGapSegment,
	depth_to_label,
	get_fine_scan_alternate_direction,
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
FLYING_SCAN_BIN_MEDIAN3 = "median3"
FLYING_SCAN_PROGRESS_EVERY = 25
FLYING_SCAN_PROGRESS_INTERVAL_S = 1.0
FLYING_SAMPLES_FILENAME = "flying_samples.jsonl"
# tool.gantry_wait caps a single line at GANTRY_WAIT_TIMEOUT (120s); refuse a feed that cannot
# finish a line inside it instead of letting the wait time out mid-pass.
FLYING_SCAN_MAX_LINE_SECONDS = 100.0
# The drive stops reporting progress when it has finished or faulted; the sampler breaks on that
# and lets gantry_wait surface the reason.
FLYING_SCAN_STALL_S = 0.5
# Drives settle a few hundredths short of the commanded end, so an exact arrival check never fires
# and every pass waited out FLYING_SCAN_STALL_S instead. Well inside FLYING_SCAN_LEAD_OUT_MARGIN_MM,
# so the reads it drops lie past the grid anyway.
FLYING_SCAN_ARRIVAL_TOLERANCE_MM = 0.2
FLYING_SCAN_PROGRESS_EPS_MM = 0.02
# Velocity for the depth-to-position correction is a median over the trailing deltas so a single
# jittered timestamp cannot move a sample.
FLYING_SCAN_VELOCITY_WINDOW = 8
# The depth is sampled when the laser replies and the position when the encoder replies, so what
# moves a depth is the time between the two replies (the encoder delay), not how long the laser
# read itself took. Any other bus user that takes the port lock between them (the y-axis weight
# guard reads the scale every few milliseconds while y moves) stretches that delay. Each sample
# is corrected by how far its delay exceeds the fastest recent one, which is the uninterrupted
# laser-then-encoder cadence the capture latency was fitted with. A delay past this multiple of the
# trailing median is too uncertain to correct and the sample is dropped; reads past
# FLYING_SCAN_READ_TIMEOUT_S stop the pass instead. The cap never falls below the floor, so
# scheduler jitter on a fast bus keeps its samples, and the cap and the correction only apply once
# the window holds enough delays to give a meaningful median and minimum.
FLYING_SCAN_SLOW_READ_FACTOR = 3.0
FLYING_SCAN_SLOW_READ_FLOOR_S = 0.025
FLYING_SCAN_SLOW_READ_WINDOW = 16
FLYING_SCAN_SLOW_READ_MIN_SPANS = 4
# The correction shifts every depth backwards by feed * capture_latency_s, so a pass that stops at
# end_mm never captures the raw samples the final grid positions would bin from. The pass flies the
# latency shift plus this margin past the grid; the margin covers the half-step binning tolerance of
# the 1mm fine and 4mm coarse grids.
FLYING_SCAN_LEAD_OUT_MARGIN_MM = 1.0
# Samples taken during the acceleration ramp would get the wrong correction, so the pass starts
# this far before the first grid position: the ramp distance derived from the configured
# acceleration and the commanded rpm, plus this margin. Samples read during the trigger dwell are
# left out of the velocity estimate (see estimate_sample_velocity_mm_s), so the margin does not
# have to cover the dwell.
FLYING_SCAN_LEAD_IN_MARGIN_MM = 2.0
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
# The interlock is only checked between reads, and a read stuck in the controller's retry loop holds
# the port lock for the whole retry, so no stop write can reach the bus until it ends. Moving reads
# therefore run as one transaction with no retries under this Modbus timeout, and a read that fails
# or takes longer than this in total (lock wait included) stops the axis and discards the pass. That
# bounds how long the gantry can fly unobserved to about two of these per sample plus the stop write,
# instead of the client's full retry budget. A normal read takes a few tens of milliseconds, and the
# margin above that leaves room for the weight guard's 0.1s fast weigh to hold the lock first.
FLYING_SCAN_READ_TIMEOUT_S = 0.15
# A pass stopped by a read overrun is flown again; a bus that keeps overrunning fails the line.
FLYING_SCAN_MAX_READ_OVERRUN_RESTARTS = 2
# An interrupted pass resumes this far before the furthest depth it placed, so the bins around the
# stop are flown again at feed rather than taken from the deceleration.
FLYING_SCAN_RESUME_OVERLAP_MM = 5.0
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
	# Laser reply to encoder reply for a moving read, the part of the latency that varies with
	# bus traffic; the correction adds how far it exceeds the line's fastest recent one.
	encoder_delay_s: Optional[float] = None


@dataclass
class FlyingScanResample:
	"""A grid position the pass left empty, probed afterwards by stopping on it."""

	position_mm: float
	depth_mm: Optional[float]


def sample_depth_position_mm(sample: FlyingScanSample) -> float:
	"""Position the depth belongs to; corrected when the capture measured a velocity."""
	if sample.depth_position_mm is not None:
		return float(sample.depth_position_mm)

	return float(sample.position_mm)


def estimate_sample_velocity_mm_s(
	samples: list[FlyingScanSample],
	*,
	window: int = FLYING_SCAN_VELOCITY_WINDOW,
	first_moving_index: int = 0,
) -> Optional[float]:
	"""Signed trailing velocity from consecutive samples, robust to one jittered read.

	Samples before `first_moving_index` were read while the drive sat in its trigger dwell. Their
	zero deltas would hold the median down for most of a window after the axis starts and leave
	the first grid positions under-corrected, so they never enter it.
	"""
	recent_samples = samples[max(int(first_moving_index), len(samples) - window):]

	if len(recent_samples) < 2:
		return None

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
	"""Longest encoder delay whose depth can still be placed by the delay correction."""
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
	# Moving reads discarded because the encoder answered too long after the laser to correct.
	dropped_slow_reads: int = 0
	# Latency each depth_position_mm was corrected with, so a replay can undo or redo it.
	capture_latency_s: Optional[float] = None
	# Bins filled by a stepped probe after the pass. They update the grid values but are not
	# samples of the pass, so they stay out of `samples` and are logged with their own source.
	resamples: list[FlyingScanResample] = field(default_factory=list)
	# Rebinned from an earlier pass over the same line instead of flown; its samples are that
	# pass's, so the sample log keeps them only once.
	is_reused: bool = False

	def record_resample(self, position_mm: float, depth_mm: Optional[float]) -> None:
		self.resamples.append(
			FlyingScanResample(
				position_mm=float(position_mm),
				depth_mm=None if depth_mm is None else float(depth_mm),
			)
		)

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


def grid_step_mm(positions: list[float]) -> float:
	"""The grid's regular step. An axis grid ends on its bound, so its last step can be short, and
	a grid flown in reverse starts with that step; reading the step off the first pair would
	shrink every bin to it."""
	steps = sorted(abs(later - earlier) for earlier, later in zip(positions, positions[1:]))

	return float(steps[len(steps) // 2])


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
	one. `median` summarizes every sample that falls in a grid cell, for noisy sensors, but
	pools a whole step and blurs edges. `median3` takes the median of the three samples nearest
	the grid position, which matches the stepped scan's three-read median without widening the
	window; a cell with fewer than three samples keeps its nearest one, because the median of two
	readings straddling an edge is a depth neither side has.
	"""
	if len(positions) == 0:
		return []

	if mode not in (FLYING_SCAN_BIN_NEAREST, FLYING_SCAN_BIN_MEDIAN, FLYING_SCAN_BIN_MEDIAN3):
		raise ValueError(f"Unsupported flying scan bin mode: {mode}")

	if max_distance_mm is None:
		max_distance_mm = grid_step_mm(positions) if len(positions) > 1 else float("inf")

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

	if mode == FLYING_SCAN_BIN_MEDIAN3:
		nearest_buckets: list[list[tuple[float, float]]] = [[] for _ in positions]

		for sample in samples:
			if sample.depth_mm is None:
				continue

			sample_position_mm = sample_depth_position_mm(sample)
			index = find_index(sample_position_mm)
			distance_mm = abs(sample_position_mm - positions[index])

			if distance_mm > max_distance_mm:
				continue

			nearest_buckets[index].append((distance_mm, float(sample.depth_mm)))

		median3_values: list[Optional[float]] = []

		for bucket in nearest_buckets:
			bucket.sort(key=lambda entry: entry[0])

			if len(bucket) >= 3:
				median3_values.append(float(median(depth for _, depth in bucket[:3])))
			elif len(bucket) > 0:
				median3_values.append(bucket[0][1])
			else:
				median3_values.append(None)

		return median3_values

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


def estimate_axis_ramp_mm(motor: Any, rpm: int) -> Optional[float]:
	"""Distance the axis covers accelerating from rest to the commanded rpm.

	The drive's acceleration is a time in ms per 1000rpm, so the ramp to `rpm` takes
	accel * rpm / 1e6 seconds at a constant acceleration, covering half of feed * ramp time.
	"""
	params = getattr(motor, "_params", None)
	accel_ms = getattr(params, "accel", None)
	feed_mm_s = estimate_axis_feed_mm_s(motor, rpm)

	if not isinstance(accel_ms, (int, float)) or float(accel_ms) <= 0.0 or feed_mm_s is None:
		return None

	ramp_s = float(accel_ms) * float(rpm) / 1_000_000.0

	return 0.5 * feed_mm_s * ramp_s


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
	"""Keep the raw moving samples so cadence, feed and edge sharpness can be reviewed.

	Bins probed by stepping after the pass are appended with `source="resampled"`, so an offline
	look at a run can tell a flown bin from a resampled one. Call it after resampling.
	"""
	if run_dir is None:
		return None

	output_path = Path(run_dir) / FLYING_SAMPLES_FILENAME
	output_path.parent.mkdir(parents=True, exist_ok=True)
	line_fields = {
		"label": label,
		"axis": line.axis,
		"fixed_mm": line.fixed_mm,
		"start_mm": line.start_mm,
		"end_mm": line.end_mm,
		"rpm": line.rpm,
		"capture_latency_s": line.capture_latency_s,
	}

	with output_path.open("a", encoding="utf-8") as output_file:
		for sample in [] if line.is_reused is True else line.samples:
			output_file.write(
				json.dumps(
					{
						**line_fields,
						"source": "flown",
						"position_mm": sample.position_mm,
						"depth_position_mm": sample.depth_position_mm,
						"velocity_mm_s": sample.velocity_mm_s,
						"encoder_delay_s": sample.encoder_delay_s,
						"depth_mm": sample.depth_mm,
						"elapsed_s": sample.elapsed_s,
					}
				)
				+ "\n"
			)

		# A stopped probe has no velocity or latency shift: its depth sits at the grid position.
		for resample in line.resamples:
			output_file.write(
				json.dumps(
					{
						**line_fields,
						"source": "resampled",
						"position_mm": resample.position_mm,
						"depth_position_mm": resample.position_mm,
						"velocity_mm_s": 0.0,
						"depth_mm": resample.depth_mm,
						"elapsed_s": None,
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
	"""A safety guard engaged while a pass was sampling; the pass's samples ride along."""

	partial_samples: list["FlyingScanSample"] = []


class FlyingScanReadOverrun(TimeoutError):
	"""A moving read failed or outlasted FLYING_SCAN_READ_TIMEOUT_S, so the axis was stopped."""

	partial_samples: list["FlyingScanSample"] = []


def _is_safety_blocked(*motors: Any) -> bool:
	# Motor.wait_for_move only pauses for the guard while something waits on the move, and a
	# flying pass defers that wait until acquisition ends, so the sampler checks the interlock
	# between reads. The guard is a software interlock: it sets a flag and the stop goes out as
	# a Modbus write, so how fast the axis stops depends on how long a read can hold the bus.
	# FLYING_SCAN_READ_TIMEOUT_S bounds that. If a hardware interlock cuts drive power on the
	# guard, it stops the axis whatever the bus is doing, and this bound no longer matters.
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


async def _read_bounded(label: str, read: Callable[[], Awaitable[Any]]) -> Any:
	"""Run one moving read, raising FlyingScanReadOverrun if it fails or outlasts the bound.

	The read is never cancelled mid-transaction: a reply landing after the request was abandoned
	would be taken as the answer to the next one. The Modbus timeout bounds the transaction itself,
	and the elapsed check catches the time spent waiting on another holder of the port lock.
	"""
	started_at = time.monotonic()

	try:
		value = await read()
	except (CommunicationError, MotorError, asyncio.TimeoutError) as exc:
		raise FlyingScanReadOverrun(
			f"{label} read failed after {time.monotonic() - started_at:.3f}s: {exc}"
		) from exc

	span_s = time.monotonic() - started_at

	if span_s > FLYING_SCAN_READ_TIMEOUT_S:
		raise FlyingScanReadOverrun(
			f"{label} read took {span_s:.3f}s, over {FLYING_SCAN_READ_TIMEOUT_S:.3f}s"
		)

	return value


async def _restore_axis_velocity(scan_motor: Any) -> None:
	"""Write the configured feed back after a pass commanded its own rpm.

	Motor moves and jogs now write their own Velocity, so this only keeps the persistent register
	honest for anything that reads it between moves.
	"""
	set_param = getattr(scan_motor, "set_param", None)
	configured_rpm = getattr(getattr(scan_motor, "_params", None), "rpm", None)

	if set_param is None or not isinstance(configured_rpm, (int, float)):
		return

	await set_param("Velocity", int(configured_rpm))


class FlyingScanStopFailed(RuntimeError):
	"""The axes could not be confirmed stopped after a pass failed, so it is not flown again."""


async def _stop_flying_axes(tool: Any, scan_motor: Any, hold_motor: Any) -> list[str]:
	"""Stop both axes after a failed pass; return the axes whose stop write failed twice.

	The drive can already be triggered while the motor has not registered its pending move yet,
	so a plain stop goes out first; gantry_wait then clears the pending state it may know about.
	Leaving pending motion behind would make the next move look like this one's completion. A
	pass usually fails because the bus misbehaved, which is exactly when a stop write can fail
	too, so a failed stop is retried once and reported rather than swallowed.
	"""
	failed_motors: list[tuple[str, Any]] = []

	for label, motor in (("scan", scan_motor), ("hold", hold_motor)):
		try:
			await motor.stop()
		except Exception:
			failed_motors.append((label, motor))

	gantry_wait = getattr(tool, "gantry_wait", None)

	if gantry_wait is not None:
		await asyncio.gather(
			gantry_wait(should_abort=lambda: True),
			return_exceptions=True,
		)

	stop_failures: list[str] = []

	for label, motor in failed_motors:
		try:
			await motor.stop()
		except Exception as exc:
			stop_failures.append(f"{label} axis: {exc!r}")

	return stop_failures


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
	"""Fly one line, resuming it after a safety guard or a read overrun stops the pass.

	Every sample carries its own latency-corrected position, so the samples a stopped pass placed
	at feed stay valid: the line keeps them and flies on from just before the furthest one. A line
	with a stateful stop predicate restarts from start_mm instead, since the predicate cannot be
	replayed across the splice. A read overrun stops the line for good after
	FLYING_SCAN_MAX_READ_OVERRUN_RESTARTS restarts.
	"""
	printer = progress_printer if progress_printer is not None else print_status
	read_overrun_restarts = 0
	direction = 1.0 if float(end_mm) >= float(start_mm) else -1.0
	can_splice = stop_when is None
	pass_start_mm = float(start_mm)
	kept_samples: list[FlyingScanSample] = []
	kept_duration_s = 0.0

	def didKeepPartialPass(exc: BaseException) -> str:
		nonlocal pass_start_mm, kept_samples, kept_duration_s

		partial_samples = list(getattr(exc, "partial_samples", []))

		if can_splice is False or len(partial_samples) == 0:
			kept_samples = []
			pass_start_mm = float(start_mm)
			return "restart=from_start"

		is_first_pass = len(kept_samples) == 0 and pass_start_mm == float(start_mm)
		placed = [
			sample
			for sample in partial_samples
			if is_first_pass or direction * (sample_depth_position_mm(sample) - pass_start_mm) >= 0.0
		]
		reached_mm = [sample_depth_position_mm(sample) for sample in placed]

		if len(reached_mm) == 0:
			return f"restart=from {pass_start_mm:.2f}"

		furthest_mm = max(reached_mm) if direction > 0 else min(reached_mm)
		resume_mm = furthest_mm - direction * FLYING_SCAN_RESUME_OVERLAP_MM

		if direction * (resume_mm - pass_start_mm) <= 0.0:
			return f"restart=from {pass_start_mm:.2f}"

		kept_samples = kept_samples + [
			sample for sample in placed if direction * (sample_depth_position_mm(sample) - resume_mm) < 0.0
		]
		kept_duration_s += partial_samples[-1].elapsed_s
		pass_start_mm = resume_mm

		return f"resume=from {resume_mm:.2f} kept_samples={len(kept_samples)}"

	while True:
		try:
			line = await _scan_axis_flying_pass(
				tool=tool,
				axis=axis,
				start_mm=pass_start_mm,
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

			if len(kept_samples) > 0:
				line.samples = kept_samples + [
					sample
					for sample in line.samples
					if direction * (sample_depth_position_mm(sample) - pass_start_mm) >= 0.0
				]
				line.start_mm = float(start_mm)
				line.duration_s += kept_duration_s

			return line
		except FlyingScanSafetyInterrupted as exc:
			resume_text = didKeepPartialPass(exc)
			printer(f"flying_line={progress_label} safety_guard=stopped {resume_text} after_clear")
			await _wait_for_safety_clear(
				getattr(tool, "_mx", None),
				getattr(tool, "_my", None),
				should_abort=should_abort,
			)
		except FlyingScanReadOverrun as exc:
			if read_overrun_restarts >= FLYING_SCAN_MAX_READ_OVERRUN_RESTARTS:
				raise

			read_overrun_restarts += 1
			resume_text = didKeepPartialPass(exc)
			printer(
				f"flying_line={progress_label} read_overrun=stopped "
				f"restart={read_overrun_restarts}/{FLYING_SCAN_MAX_READ_OVERRUN_RESTARTS} {resume_text} ({exc})"
			)
			# The guard may be why the bus went quiet; never restart into it.
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

	lead_in_mm = 0.0
	ramp_mm = estimate_axis_ramp_mm(scan_motor, rpm)

	if ramp_mm is not None:
		lead_in_mm = ramp_mm + FLYING_SCAN_LEAD_IN_MARGIN_MM

	motion_start_mm = float(start_mm) - direction * lead_in_mm
	motion_end_mm = float(end_mm) + direction * lead_out_mm
	scan_params = getattr(scan_motor, "_params", None)
	lower_lim_mm = getattr(scan_params, "lower_lim", None)
	upper_lim_mm = getattr(scan_params, "lim", None)

	if isinstance(lower_lim_mm, (int, float)) and isinstance(upper_lim_mm, (int, float)):
		motion_start_mm = min(max(motion_start_mm, float(lower_lim_mm)), float(upper_lim_mm))
		motion_end_mm = min(max(motion_end_mm, float(lower_lim_mm)), float(upper_lim_mm))

	# A lead-in the axis limit shortened still starts on (or near) the first grid position while
	# the axis is ramping, so that pass keeps the stationary start sample to cover it.
	has_full_lead_in = lead_in_mm > 0.0 and direction * (float(start_mm) - motion_start_mm) >= (
		lead_in_mm - FLYING_SCAN_PROGRESS_EPS_MM
	)

	if estimated_feed_mm_s is not None:
		estimated_duration_s = abs(motion_end_mm - motion_start_mm) / estimated_feed_mm_s

		if estimated_duration_s > FLYING_SCAN_MAX_LINE_SECONDS:
			raise ValueError(
				f"flying {axis} scan of {abs(float(end_mm) - float(start_mm)):.0f}mm at {rpm}rpm "
				f"would take ~{estimated_duration_s:.0f}s, and a line is capped at "
				f"{FLYING_SCAN_MAX_LINE_SECONDS:.0f}s; raise the rpm or shorten the bounds"
			)

	printer = progress_printer
	raise_if_cancelled(should_abort)

	await hold_motor.move_to_position_no_wait(fixed_mm)
	await scan_motor.move_to_position_no_wait(motion_start_mm)
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

	# A full lead-in brings the axis up to feed before it crosses the first grid position, and
	# the samples there are dense and correctly placed. Without one, the pass starts on the
	# first grid position, the trigger dwell eats the first millimetres of travel, and one
	# stationary sample taken before the move is commanded covers that position instead. The
	# far end is covered by the lead-out: the latency correction shifts every moving sample
	# backwards, so flying lead_out_mm past end_mm keeps raw samples under the final grid
	# positions rather than leaving their bins empty.
	start_position_mm = float(await scan_motor.get_position())

	if has_full_lead_in is False:
		start_depth_mm = await tool.get_laser_distance()
		samples.append(
			FlyingScanSample(
				position_mm=start_position_mm,
				depth_mm=None if start_depth_mm is None else float(start_depth_mm),
				elapsed_s=time.monotonic() - started_at,
			)
		)

		if find_progress_index is not None:
			covered_indices.add(find_progress_index(start_position_mm))

	# The stop predicate and progress only see samples past the lead-in: a stateful predicate
	# would otherwise track a run that started before the grid, from under-corrected samples.
	is_armed = has_full_lead_in is False
	has_moved = False
	last_position_mm = start_position_mm
	last_progress_at = started_at
	stopped_early = False
	# Encoder delay of each moving read; the history includes dropped reads so a bus that slows
	# down for good raises the cap instead of discarding the rest of the line.
	encoder_delays_s: deque[float] = deque(maxlen=FLYING_SCAN_SLOW_READ_WINDOW)
	dropped_slow_reads = 0
	# Index in `samples` of the first read after the axis left its start position; earlier reads
	# were taken during the trigger dwell and stay out of the velocity estimate.
	first_moving_index: Optional[int] = None

	try:
		did_start = await scan_motor.move_to_position_no_wait(motion_end_mm, rpm=rpm)

		while did_start is True:
			raise_if_cancelled(should_abort)

			if _is_safety_blocked(scan_motor, hold_motor) is True:
				raise FlyingScanSafetyInterrupted()

			depth_mm = await _read_bounded(
				"laser",
				lambda: tool.get_laser_distance(timeout_s=FLYING_SCAN_READ_TIMEOUT_S),
			)
			laser_replied_at = time.monotonic()
			position_mm = float(
				await _read_bounded(
					"position",
					lambda: scan_motor.get_position(timeout_s=FLYING_SCAN_READ_TIMEOUT_S),
				)
			)
			sampled_at = time.monotonic()
			encoder_delay_s = sampled_at - laser_replied_at
			slow_read_cap_s = resolve_slow_read_cap_s(encoder_delays_s)
			baseline_delay_s = (
				min(encoder_delays_s) if slow_read_cap_s is not None else encoder_delay_s
			)
			encoder_delays_s.append(encoder_delay_s)
			# Past the cap the encoder read is too late for any correction to say where the beam
			# was. Keep the read for the motion checks below but leave it out of the samples, so
			# it neither bins nor feeds the velocity or stop predicate.
			is_slow_read = slow_read_cap_s is not None and encoder_delay_s > slow_read_cap_s
			# The axis may have stopped anywhere inside the latency window before this read, so
			# no velocity correction can place its depth; the settled end sample covers the end.
			has_arrived = direction * (position_mm - motion_end_mm) >= -FLYING_SCAN_ARRIVAL_TOLERANCE_MM

			is_sample = is_slow_read is False and has_arrived is False

			if is_slow_read is True:
				dropped_slow_reads += 1

			if is_sample is True:
				sample = FlyingScanSample(
					position_mm=position_mm,
					depth_mm=None if depth_mm is None else float(depth_mm),
					elapsed_s=sampled_at - started_at,
					encoder_delay_s=encoder_delay_s,
				)
				samples.append(sample)

				if (
					first_moving_index is None
					and abs(position_mm - start_position_mm) > FLYING_SCAN_PROGRESS_EPS_MM
				):
					first_moving_index = len(samples) - 1

				velocity_mm_s: Optional[float]

				if first_moving_index is None:
					# Still in the trigger dwell: the beam is where the encoder says.
					velocity_mm_s = 0.0
				else:
					velocity_mm_s = estimate_sample_velocity_mm_s(
						samples,
						first_moving_index=first_moving_index,
					)

					# One moving read gives no delta yet; the ramp is over within a millimetre,
					# so the commanded feed is the better estimate than no correction at all.
					if velocity_mm_s is None and estimated_feed_mm_s is not None:
						velocity_mm_s = direction * estimated_feed_mm_s

				if velocity_mm_s is not None:
					extra_delay_s = max(0.0, encoder_delay_s - baseline_delay_s)
					sample.velocity_mm_s = velocity_mm_s
					depth_position_mm = position_mm - velocity_mm_s * (
						capture_latency_s + extra_delay_s
					)

					# A read just after the dwell measured its depth while the axis still stood at
					# the start, and the full feed correction would push it behind that point.
					if direction * (depth_position_mm - start_position_mm) < 0.0:
						depth_position_mm = start_position_mm

					sample.depth_position_mm = depth_position_mm

				if (
					is_armed is False
					and direction * (sample_depth_position_mm(sample) - float(start_mm)) >= 0.0
				):
					is_armed = True

				if is_armed is True and find_progress_index is not None:
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

			if (
				is_sample is True
				and is_armed is True
				and stop_when is not None
				and stop_when(samples) is True
			):
				stopped_early = True
				break

			if has_arrived is True:
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
			# feed*latency short of the final grid position and leaves its bin empty. The laser
			# reports the beam one capture latency late, so wait that out first; the depth then
			# sits at the measured position with no latency shift.
			if capture_latency_s > 0.0:
				await asyncio.sleep(capture_latency_s)

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
	except BaseException as exc:
		if isinstance(exc, (FlyingScanSafetyInterrupted, FlyingScanReadOverrun)):
			exc.partial_samples = list(samples)

		stop_failures = await _stop_flying_axes(tool, scan_motor, hold_motor)

		try:
			await _restore_axis_velocity(scan_motor)
		except Exception:
			pass

		# A guard or read-overrun interrupt is normally flown again, but not when the axes could
		# not be confirmed stopped: fail the line instead of commanding a new pass into them.
		if len(stop_failures) > 0 and isinstance(
			exc, (FlyingScanSafetyInterrupted, FlyingScanReadOverrun)
		):
			raise FlyingScanStopFailed(
				f"flying {axis} pass stopped by {type(exc).__name__} but the stop write failed "
				f"({'; '.join(stop_failures)})"
			) from exc

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
	max_distance_mm = grid_step_mm(positions) if len(positions) > 1 else None
	depth_values = bin_samples_to_positions(
		samples=line.samples,
		positions=positions,
		max_distance_mm=max_distance_mm,
		mode=bin_mode,
	)

	return depth_values, line


# Lines flown for the cell being fine-scanned, keyed by axis and fixed position. A fine scan flies
# some lines twice (the vertical seek and walk, the final horizontal and the first precision row);
# the samples of the first pass already cover the second, so it is rebinned instead of flown.
_FINE_LINE_CACHE: ContextVar[Optional[dict[tuple[str, float], FlyingScanLine]]] = ContextVar(
	"fine_line_cache",
	default=None,
)
FINE_LINE_CACHE_FIXED_EPS_MM = 0.01


@contextmanager
def reuse_flown_fine_lines() -> Iterator[None]:
	"""Keep lines flown inside this block for reuse; a nested block shares the outer cache."""
	if _FINE_LINE_CACHE.get() is not None:
		yield
		return

	token = _FINE_LINE_CACHE.set({})

	try:
		yield
	finally:
		_FINE_LINE_CACHE.reset(token)


def _find_reusable_line(axis: str, positions: list[float], fixed_mm: float) -> Optional[FlyingScanLine]:
	cache = _FINE_LINE_CACHE.get()

	if cache is None or len(positions) == 0:
		return None

	half_step_mm = grid_step_mm(positions) / 2.0 if len(positions) > 1 else 0.0
	wanted_lo_mm = min(positions[0], positions[-1]) + half_step_mm
	wanted_hi_mm = max(positions[0], positions[-1]) - half_step_mm

	for (cached_axis, cached_fixed_mm), line in cache.items():
		if cached_axis != axis or abs(cached_fixed_mm - float(fixed_mm)) > FINE_LINE_CACHE_FIXED_EPS_MM:
			continue

		if min(line.start_mm, line.end_mm) <= wanted_lo_mm and max(line.start_mm, line.end_mm) >= wanted_hi_mm:
			return line

	return None


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

	max_distance_mm = grid_step_mm(positions) if len(positions) > 1 else None
	reusable_line = _find_reusable_line(axis, positions, fixed_mm)

	if reusable_line is not None:
		reused_line = FlyingScanLine(
			axis=axis,
			fixed_mm=float(fixed_mm),
			start_mm=float(positions[0]),
			end_mm=float(positions[-1]),
			rpm=reusable_line.rpm,
			samples=reusable_line.samples,
			capture_latency_s=reusable_line.capture_latency_s,
			is_reused=True,
		)
		print_status(f"flying_line={progress_label} reused samples={len(reused_line.samples)}")

		return (
			bin_samples_to_positions(
				samples=reused_line.samples,
				positions=positions,
				max_distance_mm=max_distance_mm,
			),
			reused_line,
		)

	flight_positions = positions

	if get_fine_scan_alternate_direction(scan_config) is True and len(positions) > 1:
		current_mm = float(await scan_motor.get_position())

		if abs(current_mm - positions[-1]) < abs(current_mm - positions[0]):
			flight_positions = list(reversed(positions))

	depth_values, line = await scan_line_positions_flying(
		tool,
		axis=axis,
		positions=flight_positions,
		fixed_mm=fixed_mm,
		rpm=rpm,
		progress_label=progress_label,
		should_abort=should_abort,
		capture_latency_s=get_flying_capture_latency_s(scan_config),
	)

	if flight_positions is not positions:
		depth_values = list(reversed(depth_values))

	cache = _FINE_LINE_CACHE.get()

	if cache is not None:
		cache[(axis, float(fixed_mm))] = line

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
	# A pass that never stops early covers the whole line whichever way it flies, and the edge walk
	# below runs over the grid in its own order, so fly from the end nearer the gantry and save
	# the trip to the far end and back.
	flight_positions = y_positions
	can_fly_either_way = stop_when is None and stop_after_first_edge is False and len(y_positions) > 1
	y_motor = getattr(tool, "_my", None)

	if can_fly_either_way is True and y_motor is not None:
		current_y_mm = float(await y_motor.get_position())

		if abs(current_y_mm - y_positions[-1]) < abs(current_y_mm - y_positions[0]):
			flight_positions = list(reversed(y_positions))

	depth_values, line = await scan_line_positions_flying(
		tool,
		axis="y",
		positions=flight_positions,
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

	if flight_positions is not y_positions:
		depth_values = list(reversed(depth_values))

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
		line.record_resample(y_mm, depth_mm)

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
