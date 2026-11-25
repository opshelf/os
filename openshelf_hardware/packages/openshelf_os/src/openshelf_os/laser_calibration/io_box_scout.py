"""Measure-only laser scout of the IO box scale plate.

The front-facing laser sits laser_offset_x ahead of the gripper, so with the carriage at its
x lower limit the beam lands about 25-35mm right of the gripper's IO box pose: the box centre
and its left side are out of reach. The scout therefore measures what the beam can see and
reports it next to the hand-calibrated IO box poses, so the fixed offsets between the two can
be derived and compared across machines before anything is applied:

- a descending Y line inside the box finds the scale plate's front face: the first surface the
  beam meets coming down out of the empty box. Its bottom edge (below it the beam reads
  nothing) is the Y reference; the top edge can be chamfered, so it is only a sanity check,
- an X line across the plate face finds the plate's right edge,
- an X line above the plate finds the right edge of the box opening, the X reference,
- the face depth goes through the front wall-depth model to the gripper z a shelf face at
  that depth would get.

Nothing is written to the config. Every flown line is kept in the run directory.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Callable, Optional

from openshelf_os.storage import StorageModuleSide

from .core import (
	GAP_DEPTH_THRESHOLD_MM,
	get_fine_scan_feed_mm_s,
	get_flying_capture_latency_s,
	get_gripper_xy_correction,
	get_wall_depth_to_gripper_z_model,
	print_status,
	raise_if_cancelled,
	require_laser_offsets,
	z_offset_model_intercept_at_stage,
)
from .flying import (
	FlyingScanLine,
	resolve_axis_rpm_for_feed,
	sample_scan_position_mm,
	scan_axis_flying,
	write_flying_line_samples,
)
from .motion import enter_scan_mode

logger = logging.getLogger(__name__)
IO_BOX_PLATE_THICKNESS_MM = 8.0
IO_BOX_PLATE_THICKNESS_TOLERANCE_MM = 3.0
# Consecutive returns stay in one segment while within this of the segment's median depth.
IO_BOX_PROFILE_DEPTH_STEP_MM = 3.0
# Shorter segments are single-read noise and are left out of the profile.
IO_BOX_PROFILE_MIN_SAMPLES = 2
# Vertical line window around the hand-calibrated multi-item grab y, in the laser frame.
IO_BOX_Y_LINE_ABOVE_GRAB_MM = 60.0
IO_BOX_Y_LINE_BELOW_GRAB_MM = 160.0
# Carriage x of the vertical line: inside the box, as far left as the axis allows.
IO_BOX_Y_LINE_X_MM = 10.0
# The X lines run from the carriage's left limit to this far right of the predicted box centre.
IO_BOX_X_LINE_SPAN_RIGHT_OF_CENTER_MM = 180.0
IO_BOX_ABOVE_PLATE_LINE_MM = 40.0
# Each depth is placed feed x capture latency (~42ms) back along the line, and that latency
# varies with the bus. At the 40mm/s fine feed the reference edges spread ~1.9mm between runs
# (lb4a-3, 2026-11-25), so each is flown again slowly across this window around the fast edge.
IO_BOX_EDGE_PASS_FEED_MM_S = 5.0
IO_BOX_EDGE_PASS_HALF_WINDOW_MM = 6.0
# A side of an edge counts once this many consecutive reads agree on it.
IO_BOX_EDGE_MIN_READS = 3
IO_BOX_SCOUT_RESULT_FILENAME = "io_box_scout.json"
# Where the box sits on every lb4a within a few mm, so the search window needs no hand-set pose.
IO_BOX_NOMINAL_GRAB_MM = (23.0, 465.0)
# Fixed offsets from the measured references to each IO box pose. The scouts of 2026-11-25
# derived them from hand-calibrated poses: multi-item from lb4a-3 only (lb4a-9's was never
# calibrated), low y and Z_EXTENSION_IO as the mean of lb4a-3 (+32.9, 0.0) and lb4a-9
# (+31.4, -2.5). X follows lb4a-3: its opening edge matched lb4a-9's within 1mm while the
# hand-set x differed by 3mm.
IO_BOX_STAGE_EXTENSION_MM = 52.0
IO_BOX_X_FROM_OPENING_EDGE_MM = -83.1
IO_BOX_LOW_Y_FROM_PLATE_BOTTOM_MM = 32.1
IO_BOX_MULTI_ITEM_Y_FROM_PLATE_BOTTOM_MM = 30.9
IO_BOX_Z_IO_FROM_FACE_MM = -1.2
IO_BOX_Z_MULTI_ITEM_FROM_FACE_MM = 10.5


@dataclass
class ProfileSegment:
	"""A run of consecutive reads along a line: one surface (depth) or no return (depth None)."""

	start_mm: float
	end_mm: float
	depth_mm: Optional[float]
	sample_count: int

	@property
	def is_return(self) -> bool:
		return self.depth_mm is not None


@dataclass
class IOBoxManualPose:
	pickup_low: Optional[tuple[float, float]]
	pickup_high: Optional[tuple[float, float]]
	pickup_multi_item: Optional[tuple[float, float]]
	z_extension_io: Optional[float]
	z_extension_io_multi_item: Optional[float]
	stage_extension_io_box: Optional[float]
	stage_extension_io_box_multi_item: Optional[float]
	z_extension_place: Optional[float]


@dataclass
class IOBoxScoutResult:
	laser_offset_x_mm: float
	laser_offset_y_mm: float
	gripper_correction_x_mm: float
	gripper_correction_y_mm: float
	manual: IOBoxManualPose
	y_line_x_mm: float
	y_profile: list[ProfileSegment] = field(default_factory=list)
	plate_x_profile: list[ProfileSegment] = field(default_factory=list)
	above_plate_x_profile: list[ProfileSegment] = field(default_factory=list)
	plate_top_laser_y_mm: Optional[float] = None
	plate_bottom_laser_y_mm: Optional[float] = None
	# The fast-pass edges the slow passes refined, kept to show how far they moved.
	plate_bottom_fast_laser_y_mm: Optional[float] = None
	opening_right_edge_fast_laser_x_mm: Optional[float] = None
	plate_thickness_mm: Optional[float] = None
	plate_thickness_matches: Optional[bool] = None
	plate_face_depth_mm: Optional[float] = None
	plate_right_edge_laser_x_mm: Optional[float] = None
	opening_right_edge_laser_x_mm: Optional[float] = None
	# Tool frame: the gantry position that puts the gripper where the beam measured.
	plate_top_tool_y_mm: Optional[float] = None
	plate_bottom_tool_y_mm: Optional[float] = None
	plate_right_edge_tool_x_mm: Optional[float] = None
	opening_right_edge_tool_x_mm: Optional[float] = None
	# Gripper z a shelf face at the plate's depth would get, at the IO box stage extension.
	face_equivalent_z_mm: Optional[float] = None
	# The pose the fixed offsets give for this machine. Proposed only: nothing is written.
	proposed_x_mm: Optional[float] = None
	proposed_low_y_mm: Optional[float] = None
	proposed_multi_item_y_mm: Optional[float] = None
	proposed_z_io_mm: Optional[float] = None
	proposed_z_multi_item_mm: Optional[float] = None
	proposed_stage_extension_mm: float = IO_BOX_STAGE_EXTENSION_MM
	# Hand-calibrated value minus the measured reference: the fixed offsets to compare across machines.
	multi_item_x_minus_opening_edge_mm: Optional[float] = None
	low_x_minus_opening_edge_mm: Optional[float] = None
	multi_item_y_minus_plate_bottom_mm: Optional[float] = None
	low_y_minus_plate_bottom_mm: Optional[float] = None
	high_y_minus_plate_bottom_mm: Optional[float] = None
	z_io_minus_face_equivalent_mm: Optional[float] = None
	z_multi_item_minus_face_equivalent_mm: Optional[float] = None
	notes: list[str] = field(default_factory=list)

	def to_dict(self) -> dict[str, Any]:
		return asdict(self)


def _is_no_return(depth_mm: float) -> bool:
	return depth_mm <= GAP_DEPTH_THRESHOLD_MM


def segment_depth_profile(
	samples: list[Any],
	*,
	depth_step_mm: float = IO_BOX_PROFILE_DEPTH_STEP_MM,
	min_samples: int = IO_BOX_PROFILE_MIN_SAMPLES,
) -> list[ProfileSegment]:
	"""Split a flown line into surfaces and no-return runs, in flight order.

	Failed reads (depth None) carry no information and are skipped. Segment ends are the
	midpoints between the last read of one run and the first read of the next.
	"""
	reads = [
		(sample_scan_position_mm(sample), float(sample.depth_mm))
		for sample in samples
		if sample.depth_mm is not None
	]
	runs: list[list[tuple[float, float]]] = []

	for position_mm, depth_mm in reads:
		if len(runs) > 0:
			run = runs[-1]
			run_is_gap = _is_no_return(run[0][1])

			if _is_no_return(depth_mm) is run_is_gap and (
				run_is_gap is True
				or abs(depth_mm - median(read[1] for read in run)) <= depth_step_mm
			):
				run.append((position_mm, depth_mm))
				continue

		runs.append([(position_mm, depth_mm)])

	runs = [run for run in runs if len(run) >= min_samples]
	segments: list[ProfileSegment] = []

	for index, run in enumerate(runs):
		start_mm = run[0][0]
		end_mm = run[-1][0]

		if index > 0:
			start_mm = (runs[index - 1][-1][0] + run[0][0]) / 2.0

		if index < len(runs) - 1:
			end_mm = (run[-1][0] + runs[index + 1][0][0]) / 2.0

		segments.append(
			ProfileSegment(
				start_mm=float(start_mm),
				end_mm=float(end_mm),
				depth_mm=None if _is_no_return(run[0][1]) else float(median(read[1] for read in run)),
				sample_count=len(run),
			)
		)

	return segments


def find_plate_in_descending_profile(segments: list[ProfileSegment]) -> Optional[ProfileSegment]:
	"""The first surface below the open box, ending in a drop to no return.

	`segments` run top to bottom and start inside the empty box, where the beam reads nothing.
	Parts under the plate can return the beam too, so the plate is the first surface down, not
	the one above the longest gap.
	"""
	if len(segments) < 3 or segments[0].is_return is True:
		return None

	plate = segments[1]

	if plate.is_return is False or segments[2].is_return is True:
		return None

	return plate


def find_opening_right_edge(segments: list[ProfileSegment]) -> Optional[float]:
	"""Right end of the no-return run the line starts in, inside the box opening."""
	ordered = sorted(segments, key=lambda segment: min(segment.start_mm, segment.end_mm))

	if len(ordered) < 2 or ordered[0].is_return is True:
		return None

	return max(ordered[0].start_mm, ordered[0].end_mm)


def find_right_edge_in_profile(
	segments: list[ProfileSegment],
	*,
	face_depth_mm: float,
	depth_step_mm: float = IO_BOX_PROFILE_DEPTH_STEP_MM,
) -> Optional[float]:
	"""End of the first plate-face segment on a left-to-right line, if the line leaves the face."""
	for index, segment in enumerate(segments):
		if segment.is_return is False or abs(float(segment.depth_mm) - face_depth_mm) > depth_step_mm:
			continue

		if index == len(segments) - 1:
			return None

		return float(segment.end_mm)

	return None


def find_return_transition(
	samples: list[Any],
	*,
	to_return: bool,
	min_reads: int = IO_BOX_EDGE_MIN_READS,
) -> Optional[float]:
	"""Midpoint of the first change between no return and a surface, in flight order.

	`to_return` False finds a surface ending (the plate's bottom edge flown downward), True
	finds a surface starting (the opening's edge flown out of the box). Both sides must hold
	for `min_reads` consecutive reads, so one stray read cannot place the edge.
	"""
	reads = [
		(sample_scan_position_mm(sample), float(sample.depth_mm) > GAP_DEPTH_THRESHOLD_MM)
		for sample in samples
		if sample.depth_mm is not None
	]

	for index in range(min_reads, len(reads) - min_reads + 1):
		before = reads[index - min_reads : index]
		after = reads[index : index + min_reads]

		if all(is_return is not to_return for _, is_return in before) and all(
			is_return is to_return for _, is_return in after
		):
			return (before[-1][0] + after[0][0]) / 2.0

	return None


def _pair(value: Any) -> Optional[tuple[float, float]]:
	if value is None:
		return None

	return float(value[0]), float(value[1])


def _optional_float(value: Any) -> Optional[float]:
	return None if value is None else float(value)


async def read_manual_io_box_pose(tool: Any) -> IOBoxManualPose:
	async def param(name: str) -> Any:
		return await tool.get_param(name)

	return IOBoxManualPose(
		pickup_low=_pair(await param("PICKUP_POSITION_L")),
		pickup_high=_pair(await param("PICKUP_POSITION_H")),
		pickup_multi_item=_pair(await param("PICKUP_POSITION_MULTI_ITEM")),
		z_extension_io=_optional_float(await param("Z_EXTENSION_IO")),
		z_extension_io_multi_item=_optional_float(await param("Z_EXTENSION_IO_MULTI_ITEM")),
		stage_extension_io_box=_optional_float(await param("STAGE_EXTENSION_IO_BOX")),
		stage_extension_io_box_multi_item=_optional_float(await param("STAGE_EXTENSION_IO_BOX_MULTI_ITEM")),
		z_extension_place=_optional_float(await param("Z_EXTENSION_PLACE")),
	)


def _difference(left: Optional[float], right: Optional[float]) -> Optional[float]:
	if left is None or right is None:
		return None

	return float(left) - float(right)


def _face_equivalent_z(
	*,
	model: Any,
	depth_mm: Optional[float],
	tool_x_mm: Optional[float],
	stage_mm: Optional[float],
	z_place_mm: Optional[float],
) -> Optional[float]:
	if model is None or depth_mm is None or stage_mm is None or z_place_mm is None:
		return None

	intercept_mm = z_offset_model_intercept_at_stage(model=model, stage_extension_mm=float(stage_mm))
	slope_mm = float(model.x_slope_mm_per_mm) * float(tool_x_mm or 0.0)

	return float(z_place_mm) + float(depth_mm) + intercept_mm + slope_mm


def summarize_io_box_scout(result: IOBoxScoutResult, *, z_model: Any) -> None:
	"""Fill the tool-frame values and the manual-minus-measured offsets in place."""
	manual = result.manual

	if result.plate_top_laser_y_mm is not None:
		result.plate_top_tool_y_mm = (
			result.plate_top_laser_y_mm - result.laser_offset_y_mm + result.gripper_correction_y_mm
		)

	if result.plate_bottom_laser_y_mm is not None:
		result.plate_bottom_tool_y_mm = (
			result.plate_bottom_laser_y_mm - result.laser_offset_y_mm + result.gripper_correction_y_mm
		)

	laser_to_tool_x_mm = result.laser_offset_x_mm + result.gripper_correction_x_mm

	if result.plate_right_edge_laser_x_mm is not None:
		result.plate_right_edge_tool_x_mm = result.plate_right_edge_laser_x_mm + laser_to_tool_x_mm

	if result.opening_right_edge_laser_x_mm is not None:
		result.opening_right_edge_tool_x_mm = result.opening_right_edge_laser_x_mm + laser_to_tool_x_mm

	if result.opening_right_edge_tool_x_mm is not None:
		result.proposed_x_mm = result.opening_right_edge_tool_x_mm + IO_BOX_X_FROM_OPENING_EDGE_MM

	if result.plate_bottom_tool_y_mm is not None:
		result.proposed_low_y_mm = result.plate_bottom_tool_y_mm + IO_BOX_LOW_Y_FROM_PLATE_BOTTOM_MM
		result.proposed_multi_item_y_mm = result.plate_bottom_tool_y_mm + IO_BOX_MULTI_ITEM_Y_FROM_PLATE_BOTTOM_MM

	result.face_equivalent_z_mm = _face_equivalent_z(
		model=z_model,
		depth_mm=result.plate_face_depth_mm,
		tool_x_mm=result.proposed_x_mm if result.proposed_x_mm is not None else IO_BOX_NOMINAL_GRAB_MM[0],
		stage_mm=IO_BOX_STAGE_EXTENSION_MM,
		z_place_mm=manual.z_extension_place,
	)

	if result.face_equivalent_z_mm is not None:
		result.proposed_z_io_mm = result.face_equivalent_z_mm + IO_BOX_Z_IO_FROM_FACE_MM
		result.proposed_z_multi_item_mm = result.face_equivalent_z_mm + IO_BOX_Z_MULTI_ITEM_FROM_FACE_MM

	multi_item = manual.pickup_multi_item
	low = manual.pickup_low
	high = manual.pickup_high
	result.multi_item_x_minus_opening_edge_mm = _difference(
		None if multi_item is None else multi_item[0], result.opening_right_edge_tool_x_mm
	)
	result.low_x_minus_opening_edge_mm = _difference(None if low is None else low[0], result.opening_right_edge_tool_x_mm)
	result.multi_item_y_minus_plate_bottom_mm = _difference(
		None if multi_item is None else multi_item[1], result.plate_bottom_tool_y_mm
	)
	result.low_y_minus_plate_bottom_mm = _difference(None if low is None else low[1], result.plate_bottom_tool_y_mm)
	result.high_y_minus_plate_bottom_mm = _difference(None if high is None else high[1], result.plate_bottom_tool_y_mm)
	result.z_io_minus_face_equivalent_mm = _difference(manual.z_extension_io, result.face_equivalent_z_mm)
	result.z_multi_item_minus_face_equivalent_mm = _difference(
		manual.z_extension_io_multi_item, result.face_equivalent_z_mm
	)


def _profile_text(label: str, segments: list[ProfileSegment]) -> str:
	parts = [
		f"{segment.start_mm:.1f}..{segment.end_mm:.1f}:"
		+ ("none" if segment.depth_mm is None else f"{segment.depth_mm:.1f}")
		for segment in segments
	]
	return f"{label}_profile=" + " ".join(parts)


async def run_io_box_scout(
	*,
	robot: Any,
	scan_config: Any,
	run_dir: Optional[Path],
	x_travel_min_mm: float,
	x_travel_max_mm: float,
	y_travel_min_mm: float,
	y_travel_max_mm: float,
	should_abort: Optional[Callable[[], bool]] = None,
) -> IOBoxScoutResult:
	"""Fly the IO box lines with the door open and return the measurements; moves, never saves."""
	tool = robot._tool
	io_box = getattr(robot, "_io", None)

	if io_box is None:
		raise ValueError("IO box is not configured")

	laser_offset_x_mm, laser_offset_y_mm = require_laser_offsets(scan_config)
	correction = get_gripper_xy_correction(scan_config=scan_config, side=StorageModuleSide.FRONT) or (0.0, 0.0)
	z_model = get_wall_depth_to_gripper_z_model(scan_config=scan_config, side=StorageModuleSide.FRONT)
	manual = await read_manual_io_box_pose(tool)
	# An uncalibrated machine carries 0,0 poses; search around where the box always is instead.
	prior = next(
		(pose for pose in (manual.pickup_multi_item, manual.pickup_low) if pose is not None and pose[1] > 0.0),
		IO_BOX_NOMINAL_GRAB_MM,
	)

	result = IOBoxScoutResult(
		laser_offset_x_mm=float(laser_offset_x_mm),
		laser_offset_y_mm=float(laser_offset_y_mm),
		gripper_correction_x_mm=float(correction[0]),
		gripper_correction_y_mm=float(correction[1]),
		manual=manual,
		y_line_x_mm=0.0,
	)

	if z_model is None:
		result.notes.append("no front wall-depth z model: z is not converted")

	prior_laser_x_mm = prior[0] - result.laser_offset_x_mm - result.gripper_correction_x_mm
	prior_laser_y_mm = prior[1] + result.laser_offset_y_mm - result.gripper_correction_y_mm
	y_line_x_mm = min(max(IO_BOX_Y_LINE_X_MM, x_travel_min_mm), x_travel_max_mm)
	result.y_line_x_mm = y_line_x_mm
	y_top_mm = min(prior_laser_y_mm + IO_BOX_Y_LINE_ABOVE_GRAB_MM, y_travel_max_mm)
	y_bottom_mm = max(prior_laser_y_mm - IO_BOX_Y_LINE_BELOW_GRAB_MM, y_travel_min_mm)
	x_left_mm = x_travel_min_mm
	x_right_mm = min(prior_laser_x_mm + IO_BOX_X_LINE_SPAN_RIGHT_OF_CENTER_MM, x_travel_max_mm)
	feed_mm_s = get_fine_scan_feed_mm_s(scan_config)
	capture_latency_s = get_flying_capture_latency_s(scan_config)
	y_rpm = resolve_axis_rpm_for_feed(tool._my, feed_mm_s)
	x_rpm = resolve_axis_rpm_for_feed(tool._mx, feed_mm_s)
	slow_y_rpm = resolve_axis_rpm_for_feed(tool._my, IO_BOX_EDGE_PASS_FEED_MM_S)
	slow_x_rpm = resolve_axis_rpm_for_feed(tool._mx, IO_BOX_EDGE_PASS_FEED_MM_S)

	if y_rpm is None or x_rpm is None or slow_y_rpm is None or slow_x_rpm is None:
		raise ValueError("The gantry axes need a configured mm per revolution for a flying scan")

	print_status(
		f"phase=io-box-scout prior_laser=({prior_laser_x_mm:.2f},{prior_laser_y_mm:.2f}) "
		f"y_line=x:{y_line_x_mm:.2f} y:({y_top_mm:.2f},{y_bottom_mm:.2f}) "
		f"x_lines=({x_left_mm:.2f},{x_right_mm:.2f})"
	)

	async def didFly(
		label: str,
		axis: str,
		start_mm: float,
		end_mm: float,
		fixed_mm: float,
		*,
		is_slow: bool = False,
	) -> FlyingScanLine:
		raise_if_cancelled(should_abort)
		rpms = {("y", False): y_rpm, ("x", False): x_rpm, ("y", True): slow_y_rpm, ("x", True): slow_x_rpm}
		line = await scan_axis_flying(
			tool=tool,
			axis=axis,
			start_mm=start_mm,
			end_mm=end_mm,
			fixed_mm=fixed_mm,
			rpm=int(rpms[(axis, is_slow)]),
			progress_label=label,
			should_abort=should_abort,
			capture_latency_s=capture_latency_s,
		)
		write_flying_line_samples(run_dir=run_dir, label=label, line=line)
		return line

	did_open_door = False
	did_fail = False

	try:
		await enter_scan_mode(tool, StorageModuleSide.FRONT, scan_config)
		raise_if_cancelled(should_abort)
		print_status("phase=io-box-door-open")
		did_open_door = True
		await io_box.open_door()

		y_line = await didFly("io_box_y", "y", y_top_mm, y_bottom_mm, y_line_x_mm)
		result.y_profile = segment_depth_profile(y_line.samples)
		print_status(_profile_text("io_box_y", result.y_profile))
		plate = find_plate_in_descending_profile(result.y_profile)

		if plate is None:
			result.notes.append(
				"the vertical line did not start in the open box and drop off a first surface: plate not found"
			)
			return result

		result.plate_top_laser_y_mm = max(plate.start_mm, plate.end_mm)
		result.plate_bottom_laser_y_mm = min(plate.start_mm, plate.end_mm)
		result.plate_thickness_mm = result.plate_top_laser_y_mm - result.plate_bottom_laser_y_mm
		result.plate_thickness_matches = (
			abs(result.plate_thickness_mm - IO_BOX_PLATE_THICKNESS_MM) <= IO_BOX_PLATE_THICKNESS_TOLERANCE_MM
		)
		result.plate_face_depth_mm = plate.depth_mm

		result.plate_bottom_fast_laser_y_mm = result.plate_bottom_laser_y_mm
		bottom_line = await didFly(
			"io_box_plate_bottom_slow_y",
			"y",
			result.plate_bottom_fast_laser_y_mm + IO_BOX_EDGE_PASS_HALF_WINDOW_MM,
			max(result.plate_bottom_fast_laser_y_mm - IO_BOX_EDGE_PASS_HALF_WINDOW_MM, y_travel_min_mm),
			y_line_x_mm,
			is_slow=True,
		)
		slow_bottom_mm = find_return_transition(bottom_line.samples, to_return=False)

		if slow_bottom_mm is None:
			result.notes.append("the slow pass did not see the plate's bottom edge: the fast edge is used")
		else:
			result.plate_bottom_laser_y_mm = slow_bottom_mm
			result.plate_thickness_mm = result.plate_top_laser_y_mm - slow_bottom_mm
			result.plate_thickness_matches = (
				abs(result.plate_thickness_mm - IO_BOX_PLATE_THICKNESS_MM) <= IO_BOX_PLATE_THICKNESS_TOLERANCE_MM
			)

		if result.plate_thickness_matches is False:
			result.notes.append(
				f"the surface above the drop-off is {result.plate_thickness_mm:.1f}mm tall, "
				f"not the {IO_BOX_PLATE_THICKNESS_MM:.0f}mm plate"
			)

		plate_mid_y_mm = (result.plate_top_laser_y_mm + result.plate_bottom_laser_y_mm) / 2.0
		plate_x_line = await didFly("io_box_plate_x", "x", x_left_mm, x_right_mm, plate_mid_y_mm)
		result.plate_x_profile = segment_depth_profile(plate_x_line.samples)
		print_status(_profile_text("io_box_plate_x", result.plate_x_profile))
		result.plate_right_edge_laser_x_mm = find_right_edge_in_profile(
			result.plate_x_profile,
			face_depth_mm=float(plate.depth_mm),
		)

		if result.plate_right_edge_laser_x_mm is None:
			result.notes.append("the plate x line never left the plate face: right edge not found")

		above_y_mm = min(result.plate_top_laser_y_mm + IO_BOX_ABOVE_PLATE_LINE_MM, y_travel_max_mm)
		above_line = await didFly("io_box_above_plate_x", "x", x_right_mm, x_left_mm, above_y_mm)
		result.above_plate_x_profile = segment_depth_profile(above_line.samples)
		print_status(_profile_text("io_box_above_plate_x", result.above_plate_x_profile))
		result.opening_right_edge_laser_x_mm = find_opening_right_edge(result.above_plate_x_profile)

		if result.opening_right_edge_laser_x_mm is None:
			result.notes.append("the line above the plate did not start inside the opening: opening edge not found")
			return result

		result.opening_right_edge_fast_laser_x_mm = result.opening_right_edge_laser_x_mm
		opening_line = await didFly(
			"io_box_opening_slow_x",
			"x",
			max(result.opening_right_edge_fast_laser_x_mm - IO_BOX_EDGE_PASS_HALF_WINDOW_MM, x_travel_min_mm),
			result.opening_right_edge_fast_laser_x_mm + IO_BOX_EDGE_PASS_HALF_WINDOW_MM,
			above_y_mm,
			is_slow=True,
		)
		slow_opening_mm = find_return_transition(opening_line.samples, to_return=True)

		if slow_opening_mm is None:
			result.notes.append("the slow pass did not see the opening edge: the fast edge is used")
		else:
			result.opening_right_edge_laser_x_mm = slow_opening_mm

		return result
	except BaseException:
		did_fail = True
		raise
	finally:
		summarize_io_box_scout(result, z_model=z_model)

		if run_dir is not None:
			Path(run_dir).mkdir(parents=True, exist_ok=True)
			(Path(run_dir) / IO_BOX_SCOUT_RESULT_FILENAME).write_text(
				json.dumps(result.to_dict(), indent=2) + "\n",
				encoding="utf-8",
			)

		# An abort's own home retracts and closes the door; on a failure the cleanup must not
		# replace the error that stopped the scout.
		if should_abort is None or should_abort() is False:
			try:
				await tool.retract()

				if did_open_door is True:
					await io_box.home_door()
			except Exception:
				if did_fail is False:
					raise

				logger.warning("io_box_scout_cleanup_failed", exc_info=True)
