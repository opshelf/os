"""Z-stall probe: measure where the tool tip meets the shelf plate and fit the z model.

The laser and the gripper tip ride the same carriage, so the stage extension cancels out of
`z_contact - wall_depth`: what is left is the fixed laser-to-tip geometry. Probing the plate
beside a cell (never the cell's hole) therefore measures the one thing a laser alone cannot
report - where the gripper actually is when the rack is in front of it.

Two things make the result trustworthy and both are enforced here:

* the probe presses the shelf insert's front plate, so a rack that yields under load moves
  instead of stalling. Each point is stalled twice and a pair that disagrees is dropped, and
  any stage back-drive during a probe aborts the run outright (that is the shelf coming out
  of its latches, not a wall).
* the fit is only as good as the wall depth it subtracts, so probes are taken from cells the
  sweep measured and are skipped when the cell is a tray home, unavailable, or occupied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any, Callable

from openshelf_os.storage import CellStatus, StorageModuleSide
from .core import get_laser_sample_settle_ms, print_status
from .recording import load_candidates_from_metadata, load_json_file
from .scan import ScanDetectedCell, ScanDetectedShelf
from .staged_scan import (
	Z_DELTA_MAX_ABS_MM,
	Z_DELTA_TILT_MIN_X_SPAN_MM,
	build_candidate_lookup_by_cell,
	resolve_z_delta_model,
)

# The z axis can out-push the stage it is mounted on (z lead 16.1mm/rev vs stage 50mm/rev), so
# a probe that does not stop the gripper back-drives the stage instead. Past this much travel
# the contact is the rack yielding, not a wall.
STAGE_BACKDRIVE_TOLERANCE_MM = 1.5


@dataclass
class ZStallSample:
	shelf_idx: int
	cell_idx: int
	probe_x_mm: float
	probe_y_mm: float
	wall_depth_mm: float
	z_contact_mm: float
	z_contact_repeat_mm: float | None
	delta_mm: float
	reason: str

	@property
	def repeat_delta_mm(self) -> float | None:
		if self.z_contact_repeat_mm is None:
			return None

		return self.z_contact_repeat_mm - self.z_contact_mm


@dataclass
class ZStallResult:
	side: StorageModuleSide
	intercept_mm: float
	slope_per_mm: float
	fit_source: str
	stage_extension_mm: float
	z_place_mm: float
	rotation_center_deg: float
	samples: list[ZStallSample] = field(default_factory=list)
	skipped: list[str] = field(default_factory=list)
	dropped_repeats: list[str] = field(default_factory=list)

	@property
	def sample_count(self) -> int:
		return len(self.samples)

	@property
	def x_span_mm(self) -> float:
		if len(self.samples) < 2:
			return 0.0

		xs = [sample.probe_x_mm for sample in self.samples]

		return max(xs) - min(xs)


def is_probe_reachable(
	cell: ScanDetectedCell,
	max_probe_tool_x_mm: float | None,
	min_probe_tool_x_mm: float | None = None,
) -> bool:
	"""Whether the probe point this cell would use is inside the carriage's travel.

	The axis clamps silently, so a probe past either limit lands somewhere it was never
	aimed: the tool ends up over the cell's tray hole instead of the divider next to it.
	"""
	max_reach_mm = max_probe_tool_x_mm
	min_reach_mm = min_probe_tool_x_mm

	if max_reach_mm is None and min_reach_mm is None:
		return True

	tool_x_mm = float(cell.tool_x_mm)

	if max_reach_mm is not None and tool_x_mm > float(max_reach_mm):
		print_status(
			f"z_stall_anchor=skip_unreachable probe={cell.shelf_idx}:{cell.cell_idx} "
			f"tool_x_mm={tool_x_mm:.1f} max_probe_tool_x_mm={float(max_reach_mm):.1f}"
		)
		return False

	if min_reach_mm is not None and tool_x_mm < float(min_reach_mm):
		print_status(
			f"z_stall_anchor=skip_unreachable probe={cell.shelf_idx}:{cell.cell_idx} "
			f"tool_x_mm={tool_x_mm:.1f} min_probe_tool_x_mm={float(min_reach_mm):.1f}"
		)
		return False

	return True


def select_z_stall_probes(
	analysis: Any,
	candidate_lookup: dict[tuple[int, int], Any],
	max_probe_tool_x_mm: float | None = None,
	min_probe_tool_x_mm: float | None = None,
	excluded_cells: set[tuple[int, int]] | None = None,
) -> list[tuple[ScanDetectedCell, float]]:
	"""Pick the probe cells: each shelf's X extremes plus a middle cell.

	The extremes give the tilt span and the middle corroborates it, because `resolve_z_delta_model`
	refuses a slope that only the two ends support. Shelves are visited middle first, so the
	first shelf that can support a tilt is the one probed before the fit is settled.
	"""
	shelves = [shelf for shelf in analysis.shelves if len(shelf.cells) > 0]

	if len(shelves) == 0:
		return []

	excluded = set() if excluded_cells is None else set(excluded_cells)
	ordered_shelves = sorted(shelves, key=lambda shelf: float(shelf.center_y_mm))
	middle = len(ordered_shelves) // 2
	shelf_sequence: list[Any] = [ordered_shelves[middle]]
	for step in range(1, len(ordered_shelves)):
		if middle + step < len(ordered_shelves):
			shelf_sequence.append(ordered_shelves[middle + step])
		if middle - step >= 0:
			shelf_sequence.append(ordered_shelves[middle - step])

	probes: list[tuple[ScanDetectedCell, float]] = []
	seen_cells: set[tuple[int, int]] = set()
	for shelf in shelf_sequence:
		valid_cells = [
			(cell, float(candidate_lookup[(cell.shelf_idx, cell.cell_idx)].wall_depth_mm))
			for cell in sorted(shelf.cells, key=lambda cell: float(cell.center_x_mm))
			if (int(cell.shelf_idx), int(cell.cell_idx)) not in excluded
			and candidate_lookup.get((cell.shelf_idx, cell.cell_idx)) is not None
			and candidate_lookup[(cell.shelf_idx, cell.cell_idx)].wall_depth_mm is not None
			and float(candidate_lookup[(cell.shelf_idx, cell.cell_idx)].wall_depth_mm) > 0.0
			and is_probe_reachable(cell, max_probe_tool_x_mm, min_probe_tool_x_mm)
		]

		if len(valid_cells) == 0:
			continue

		picks = [valid_cells[0]]
		if len(valid_cells) >= 3:
			picks.append(valid_cells[len(valid_cells) // 2])
		picks.append(valid_cells[-1])
		for cell, wall_depth_mm in picks:
			cell_key = (cell.shelf_idx, cell.cell_idx)
			if cell_key in seen_cells:
				continue
			seen_cells.add(cell_key)
			probes.append((cell, wall_depth_mm))

	return probes


def excluded_probe_cells(
	*,
	grid: Any,
	side: StorageModuleSide,
	reserved_locations: set[tuple[str, int, int]] | None = None,
) -> set[tuple[int, int]]:
	"""Cells a probe must not press: tray homes, unavailable cells, and occupied cells.

	A tray home holds a tray whose front face stands in front of the shelf plate, and an
	occupied cell holds an item, so either one becomes the wall the tip finds - and the wall
	depth subtracted from the contact would be the plate's, not the object's.
	"""
	excluded: set[tuple[int, int]] = set()

	for shelf in grid.get_side(side):
		for cell in shelf.cells:
			if cell.status != CellStatus.AVAILABLE or cell.item is not None:
				excluded.add((int(shelf.shelf_idx), int(cell.cell_idx)))

	for reserved_side, shelf_idx, cell_idx in reserved_locations or set():
		if reserved_side == side.value:
			excluded.add((int(shelf_idx), int(cell_idx)))

	return excluded


async def run_z_stall_anchor(
	*,
	tool: Any,
	analysis: Any,
	scan_config: Any,
	side: StorageModuleSide,
	candidates: list[Any] | None = None,
	excluded_cells: set[tuple[int, int]] | None = None,
	x_axis_limit_mm: float | None = None,
	x_axis_lower_limit_mm: float | None = None,
	z_axis_limit_mm: float | None = None,
	xy_offset: tuple[float, float] | None = None,
	should_abort: Callable[[], bool] | None = None,
) -> ZStallResult:
	"""Probe the plate beside each selected cell and fit the z delta model.

	Raises ValueError when the rack moves under the probe, the tool faults on approach, or no
	contact separates the intercept from the tilt: every one of those means the fit would place
	the gripper somewhere it has never been.
	"""
	metadata = load_json_file(Path(str(analysis.metadata_path)))
	if candidates is None:
		candidates = load_candidates_from_metadata(metadata)

	candidate_lookup = build_candidate_lookup_by_cell(analysis=analysis, candidates=candidates)

	# The probe drives to (tool_x + xy_offset) + probe_x_shift, so cap tool_x by the axis limit
	# minus BOTH terms. Without this the axis clamps, the tool lands over the cell's tray hole
	# instead of the divider, and the gripper jams the tray onto itself.
	offset_x_mm = 0.0 if xy_offset is None else float(xy_offset[0])
	offset_y_mm = 0.0 if xy_offset is None else float(xy_offset[1])
	probe_x_shift_mm = float(scan_config.z_stall_probe_x_shift_mm)
	max_probe_tool_x_mm = (
		None if x_axis_limit_mm is None else float(x_axis_limit_mm) - offset_x_mm - probe_x_shift_mm
	)
	min_probe_tool_x_mm = (
		None
		if x_axis_lower_limit_mm is None
		else float(x_axis_lower_limit_mm) - offset_x_mm - probe_x_shift_mm
	)

	probes = select_z_stall_probes(
		analysis,
		candidate_lookup,
		max_probe_tool_x_mm,
		min_probe_tool_x_mm,
		excluded_cells=excluded_cells,
	)
	if len(probes) == 0:
		raise ValueError(
			"z stall anchor: no reachable detected cell with a wall depth to probe "
			"(tray homes, unavailable and occupied cells are excluded)"
		)

	# Imported here rather than at module scope: the angle subpackage pulls in this package while
	# it is still initializing, so a top-level edge closes an import cycle.
	from .angle.capture import enter_capture_mode  # noqa: PLC0415
	from .angle.robot_ops import probe_z_until_stall  # noqa: PLC0415

	side_name = "left" if side == StorageModuleSide.LEFT else "right"
	side_number = 0 if side == StorageModuleSide.LEFT else 1
	rotation_param = "ROTATION_FRONT" if side_name == "left" else "ROTATION_BACK"
	stage_param = "STAGE_EXTENSION_SHELF_FRONT" if side_name == "left" else "STAGE_EXTENSION_SHELF_BACK"

	rotation_center_value = await tool.get_param(rotation_param)
	stage_extension_value = await tool.get_param(stage_param)
	z_place = await tool.get_param("Z_EXTENSION_PLACE")
	z_move_rpm = await tool.get_param("Z_MOVE_RPM")
	z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

	if rotation_center_value is None or stage_extension_value is None:
		raise ValueError(f"{rotation_param} and {stage_param} are required for the z stall anchor")

	if z_place is None or z_move_rpm is None or z_move_accel is None:
		raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required for the z stall anchor")

	rotation_center_deg = float(rotation_center_value)
	stage_extension_mm = float(stage_extension_value)
	z_place_mm = float(z_place)
	tray_engagement_mm = float(scan_config.z_stall_tray_engagement_mm)
	probe_rpm = scan_config.z_stall_probe_rpm
	probe_target_mm = float(scan_config.z_stall_probe_target_mm)
	probe_repeats = int(getattr(scan_config, "z_stall_probe_repeats", 1))
	repeat_tolerance_mm = float(getattr(scan_config, "z_stall_probe_repeat_tolerance_mm", 0.3))
	print_status(
		f"z_stall_anchor=start side={side_name} rotation_deg={rotation_center_deg:.3f} "
		f"stage_extension_mm={stage_extension_mm:.2f} candidate_cells={len(probes)} "
		f"probe_target_mm={probe_target_mm:.1f} repeats={probe_repeats} z_axis_limit_mm="
		f"{'unknown' if z_axis_limit_mm is None else f'{z_axis_limit_mm:.1f}'}"
	)

	# When the probe target IS the axis ceiling there is no travel to spare, so a probe that runs
	# out of travel is indistinguishable from one that found no wall: both come back reason=limit.
	# Say so up front, otherwise a rack that has drifted out of reach reads as a rack with no walls.
	has_no_z_headroom = z_axis_limit_mm is not None and probe_target_mm >= float(z_axis_limit_mm)

	if has_no_z_headroom is True:
		print_status(
			f"z_stall_anchor=no_z_headroom probe_target_mm={probe_target_mm:.1f} "
			f"z_axis_limit_mm={float(z_axis_limit_mm):.1f} "
			"reason=limit cannot distinguish 'no wall' from 'wall beyond Z travel'"
		)

	samples: list[ZStallSample] = []
	skipped: list[str] = []
	dropped_repeats: list[str] = []
	last_reason = "none"
	limit_miss_count = 0

	for detected_cell, probe_wall_depth_mm in probes:
		cell_key = (int(detected_cell.shelf_idx), int(detected_cell.cell_idx))
		# Aim from the CORRECTED tool position (tool_x + xy_offset), not the raw detection frame:
		# xy_offset is what actually puts the tool on the cell, and ignoring it can drop the probe
		# into a neighbouring tray hole instead of onto the divider.
		probe_x_mm = float(detected_cell.tool_x_mm) + offset_x_mm + probe_x_shift_mm
		probe_y_mm = float(detected_cell.tool_y_mm) + offset_y_mm
		contacts: list[float] = []
		probe_reason = "none"

		for _attempt in range(max(1, probe_repeats)):
			await enter_capture_mode(
				tool,
				side_name=side_name,
				side_number=side_number,
				x_mm=probe_x_mm,
				y_mm=probe_y_mm,
				absolute_rotation_deg=rotation_center_deg,
				stage_extension_mm=stage_extension_mm,
				z_mm=float(scan_config.z_stall_probe_start_mm),
				z_move_rpm=float(z_move_rpm),
				z_move_accel=float(z_move_accel),
				should_abort=should_abort,
			)

			did_stall, z_contact_mm, reason, _alarm_code = await probe_z_until_stall(
				tool,
				target_z_mm=float(scan_config.z_stall_probe_target_mm),
				retract_z_mm=float(scan_config.z_stall_probe_start_mm),
				settle_ms=get_laser_sample_settle_ms(scan_config),
				timeout_s=float(scan_config.z_stall_probe_timeout_s),
				probe_rpm=None if probe_rpm is None else int(probe_rpm),
				no_progress_s=float(scan_config.z_stall_probe_no_progress_s),
				probe_step_mm=float(scan_config.z_stall_probe_step_mm),
				fast_approach_mm=float(scan_config.z_stall_probe_fast_approach_mm),
				fast_approach_rpm=None if probe_rpm is None else int(probe_rpm),
				auto_retract=True,
				should_abort=should_abort,
			)
			last_reason = reason
			probe_reason = reason

			stage_motor = getattr(tool, "_ms", None)
			achieved_stage_mm = (
				stage_extension_mm if stage_motor is None else float(await stage_motor.get_position())
			)

			if abs(achieved_stage_mm - stage_extension_mm) > STAGE_BACKDRIVE_TOLERANCE_MM:
				raise ValueError(
					f"z stall anchor: the stage was pushed back from {stage_extension_mm:.1f}mm to "
					f"{achieved_stage_mm:.1f}mm during probe {cell_key[0]}:{cell_key[1]} "
					f"(z reached {z_contact_mm:.1f}mm, reason={reason}). The gripper is loading the rack "
					"harder than the stage can hold, so this contact is not a wall and the rack is being "
					"displaced. Refusing to continue."
				)

			if reason == "fast_approach_fault":
				raise ValueError(
					f"z stall anchor: the tool faulted during the rapid approach at z={z_contact_mm:.1f}mm "
					f"on probe {cell_key[0]}:{cell_key[1]}. Nothing should obstruct the tool before the "
					"wall; refusing to continue."
				)

			if did_stall is False:
				if reason == "limit":
					limit_miss_count += 1

				print_status(
					f"z_stall_anchor=miss probe={cell_key[0]}:{cell_key[1]} reason={reason} "
					f"z_mm={z_contact_mm:.3f}"
				)
				break

			# The rapid approach already traversed to fast_approach_mm unobstructed, so a wall cannot
			# sit closer than that. Anything shallower is the tool hitting something else.
			if z_contact_mm < float(scan_config.z_stall_probe_fast_approach_mm):
				print_status(
					f"z_stall_anchor=reject_shallow probe={cell_key[0]}:{cell_key[1]} reason={reason} "
					f"z_mm={z_contact_mm:.3f} "
					f"fast_approach_mm={float(scan_config.z_stall_probe_fast_approach_mm):.3f}"
				)
				break

			contacts.append(float(z_contact_mm))

		if len(contacts) == 0:
			skipped.append(f"{cell_key[0]}:{cell_key[1]} ({probe_reason})")
			continue

		if len(contacts) > 1 and abs(contacts[-1] - contacts[0]) > repeat_tolerance_mm:
			# Same point, same force, different answer: the plate moved between the two probes.
			message = (
				f"{cell_key[0]}:{cell_key[1]} stalled at {contacts[0]:.3f}mm then {contacts[-1]:.3f}mm "
				f"({abs(contacts[-1] - contacts[0]):.3f}mm apart, tolerance {repeat_tolerance_mm:.3f}mm)"
			)
			dropped_repeats.append(message)
			print_status(f"z_stall_anchor=unstable probe={message}")
			continue

		z_contact_mm = float(median(contacts))
		z_offset_anchor_mm = z_contact_mm - z_place_mm + tray_engagement_mm
		delta_mm = z_offset_anchor_mm - float(probe_wall_depth_mm)
		samples.append(
			ZStallSample(
				shelf_idx=cell_key[0],
				cell_idx=cell_key[1],
				probe_x_mm=probe_x_mm,
				probe_y_mm=probe_y_mm,
				wall_depth_mm=float(probe_wall_depth_mm),
				z_contact_mm=z_contact_mm,
				z_contact_repeat_mm=None if len(contacts) < 2 else float(contacts[-1]),
				delta_mm=delta_mm,
				reason=probe_reason,
			)
		)
		print_status(
			f"z_stall_anchor=sample probe={cell_key[0]}:{cell_key[1]} "
			f"x_mm={probe_x_mm:.3f} z_contact_mm={z_contact_mm:.3f} z_place_mm={z_place_mm:.3f} "
			f"tray_mm={tray_engagement_mm:.3f} wall_depth_mm={float(probe_wall_depth_mm):.3f} "
			f"anchor_z_offset_mm={z_offset_anchor_mm:.3f} delta_mm={delta_mm:.3f}"
		)

		# A trustworthy X slope must come from one shelf (same Y) with an interior corroboration
		# probe; break once a shelf yields >=3 wide-enough samples.
		shelf_sample_xs = [sample.probe_x_mm for sample in samples if sample.shelf_idx == cell_key[0]]
		if len(shelf_sample_xs) >= 3 and (
			max(shelf_sample_xs) - min(shelf_sample_xs)
		) >= Z_DELTA_TILT_MIN_X_SPAN_MM:
			break

	if len(samples) == 0:
		if len(dropped_repeats) > 0:
			raise ValueError(
				"z stall anchor: every probe stalled at a different depth on the second attempt, so the "
				"rack is moving under probe force rather than holding still: "
				f"{'; '.join(dropped_repeats)}"
			)

		if limit_miss_count == len(probes) and has_no_z_headroom is True:
			raise ValueError(
				f"z stall anchor: all {len(probes)} probes ran the Z axis to its travel limit "
				f"({float(z_axis_limit_mm):.1f}mm) without contact. The probe target equals that limit, "
				"so the wall is not necessarily absent - it may simply be beyond the tool's reach "
				f"(stage_extension_mm={stage_extension_mm:.2f}). Check that the rack is within reach "
				"before trusting this as 'no wall found'."
			)

		raise ValueError(
			f"z stall anchor: no probe cell contacted a wall across {len(probes)} probes "
			f"(last reason={last_reason}, stage_extension_mm={stage_extension_mm:.2f}, "
			f"skipped={', '.join(skipped) if skipped else 'none'})"
		)

	# A lone sample cannot separate the intercept from the tilt, so it collapses to a tilt-free
	# constant. Both the anchor and the manual-datum comparison can live with that, but the tilt is
	# worth ~2mm across a shelf, so a slope is only fitted when the samples can support one.
	if len(samples) < 2:
		intercept_mm = float(median([sample.delta_mm for sample in samples]))
		slope_per_mm = 0.0
		fit_source = "constant"
	else:
		intercept_mm, slope_per_mm = resolve_z_delta_model(
			[(sample.probe_x_mm, sample.delta_mm) for sample in samples]
		)
		if slope_per_mm == 0.0:
			fit_source = "single_shelf_constant"
		elif len(samples) >= 3:
			fit_source = "single_shelf_tilt"
		else:
			fit_source = "two_point_tilt"

	print_status(
		f"z_stall_anchor=fit side={side_name} source={fit_source} samples={len(samples)} "
		f"x_span_mm={max(sample.probe_x_mm for sample in samples) - min(sample.probe_x_mm for sample in samples):.1f} "
		f"intercept_mm={intercept_mm:.3f} slope_per_mm={slope_per_mm:.5f}"
	)

	if abs(intercept_mm) > Z_DELTA_MAX_ABS_MM:
		raise ValueError(
			f"z stall anchor: implausible z delta {intercept_mm:.1f}mm "
			f"(limit +/-{Z_DELTA_MAX_ABS_MM:.0f}mm); refusing to apply it to every cell"
		)

	return ZStallResult(
		side=side,
		intercept_mm=intercept_mm,
		slope_per_mm=slope_per_mm,
		fit_source=fit_source,
		stage_extension_mm=stage_extension_mm,
		z_place_mm=z_place_mm,
		rotation_center_deg=rotation_center_deg,
		samples=samples,
		skipped=skipped,
		dropped_repeats=dropped_repeats,
	)


def build_probe_analysis(
	*,
	metadata: dict[str, Any],
	metadata_path: Path,
) -> Any:
	"""Rebuild the detected cells a saved run recorded, for probing without rescanning.

	Only the per-cell detections and the sweep candidates are needed: the probe re-measures the
	plate itself, so the fine scan's depth arrays are deliberately not loaded. That keeps this
	usable on runs whose staged arrays cannot be replayed.
	"""
	cells_by_shelf: dict[int, list[ScanDetectedCell]] = {}

	for fine_scan in metadata.get("fine_scans", []):
		for row in fine_scan.get("detected_cells", []):
			shelf_idx = int(row["shelf_idx"])
			cells_by_shelf.setdefault(shelf_idx, []).append(
				ScanDetectedCell(
					shelf_idx=shelf_idx,
					cell_idx=int(row["cell_idx"]),
					center_x_mm=float(row["center_x_mm"]),
					center_y_mm=float(row["center_y_mm"]),
					tool_x_mm=float(row["tool_x_mm"]),
					tool_y_mm=float(row["tool_y_mm"]),
					width_mm=float(row["width_mm"]),
					height_mm=float(row["height_mm"]),
					point_count=int(row.get("point_count", 0)),
					bbox_x_min=float(row.get("bbox_x_min", row["center_x_mm"])),
					bbox_x_max=float(row.get("bbox_x_max", row["center_x_mm"])),
					bbox_y_min=float(row.get("bbox_y_min", row["center_y_mm"])),
					bbox_y_max=float(row.get("bbox_y_max", row["center_y_mm"])),
				)
			)

	shelves = [
		ScanDetectedShelf(
			shelf_idx=shelf_idx,
			center_y_mm=float(median([cell.tool_y_mm for cell in cells])),
			shelf_height_mm=max(float(cell.height_mm) for cell in cells),
			shelf_width_mm=sum(float(cell.width_mm) for cell in cells),
			cells=sorted(cells, key=lambda cell: int(cell.cell_idx)),
		)
		for shelf_idx, cells in sorted(cells_by_shelf.items())
	]

	return SimpleNamespace(shelves=shelves, metadata_path=Path(metadata_path))


def restrict_analysis(
	analysis: Any,
	*,
	shelf_idx: int | None = None,
	cells: set[tuple[int, int]] | None = None,
) -> Any:
	"""Narrow a probe analysis to the shelves or cells a partial test asked for."""
	shelves = []
	for shelf in analysis.shelves:
		if shelf_idx is not None and int(shelf.shelf_idx) != int(shelf_idx):
			continue

		selected = [
			cell
			for cell in shelf.cells
			if cells is None
			or (int(cell.shelf_idx), int(cell.cell_idx)) in cells
		]

		if len(selected) == 0:
			continue

		shelves.append(
			ScanDetectedShelf(
				shelf_idx=int(shelf.shelf_idx),
				center_y_mm=float(median([cell.tool_y_mm for cell in selected])),
				shelf_height_mm=max(float(cell.height_mm) for cell in selected),
				shelf_width_mm=sum(float(cell.width_mm) for cell in selected),
				cells=selected,
			)
		)

	return SimpleNamespace(shelves=shelves, metadata_path=getattr(analysis, "metadata_path", None))
