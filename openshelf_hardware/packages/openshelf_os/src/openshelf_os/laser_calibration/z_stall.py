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
  sweep measured and are skipped when the cell is disabled or is the hand-calibrated datum.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from types import SimpleNamespace
from typing import Any, Callable

from openshelf_os.storage import CellStatus, StorageModuleSide
from .core import (
	get_laser_sample_settle_ms,
	get_scan_xy_offset,
	get_scan_z_offset_model,
	print_status,
)
from .recording import load_candidates_from_metadata, load_json_file
from .scan import ScanDetectedCell, ScanDetectedShelf
from .staged_scan import (
	Z_DELTA_MAX_ABS_MM,
	Z_DELTA_TILT_MIN_X_SPAN_MM,
	resolve_z_delta_model,
	wall_depths_by_cell,
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
class ZStallDecision:
	"""The model a probed fit installs with: intercept in the grab frame, slope, and provenance."""

	intercept_mm: float
	slope_per_mm: float
	source: str


@dataclass
class ZStallResult:
	side: StorageModuleSide
	intercept_mm: float
	slope_per_mm: float
	fit_source: str
	stage_extension_mm: float
	z_place_mm: float
	rotation_center_deg: float
	# The X the tilt-free fit's intercept is the delta *at*, in the fit frame (the grab frame the
	# model is applied in). Recorded by the anchor: substituting a configured slope for a tilt the
	# probe could not measure must re-base the intercept by `-slope * fit_centroid_x_mm`.
	fit_centroid_x_mm: float = 0.0
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


def excluded_probe_cells(
	*,
	grid: Any,
	side: StorageModuleSide,
	scanned_cells: Iterable[tuple[int, int]] | None = None,
) -> set[tuple[int, int]]:
	"""Cells a probe must leave alone.

	Neither a reserved tray home nor a cell holding an item is excluded: a tray presents the same
	gripper interface and the same divider surface to the laser as an empty cell, and an item sits
	out of the probe's way, so pressing beside any opening measures the same plate. What is left out
	is a cell the rack does not actually have (DISABLED, i.e. beyond the carriage travel) and a
	hand-calibrated cell - that datum is the yardstick the fit is judged against, so it must stay out
	of the sample set.

	The datum is left out of the sample set only while the scope still contains an independent
	opening to judge it: `scanned_cells` are the cells the in-scope scan detected, so a scope that
	holds nothing but the manual cell - the cheapest way to verify that cell's z by contact - keeps
	the datum probeable instead of finding nothing to press. Without `scanned_cells` the whole side
	is the scope and the rule applies to the side's cells.
	"""
	excluded: set[tuple[int, int]] = set()
	datum_cells: set[tuple[int, int]] = set()
	disabled_cells: set[tuple[int, int]] = set()
	has_independent_cell = False

	for shelf in grid.get_side(side):
		for cell in shelf.cells:
			key = (int(shelf.shelf_idx), int(cell.cell_idx))

			if cell.status == CellStatus.DISABLED:
				excluded.add(key)
				disabled_cells.add(key)
			elif getattr(cell, "is_manual", False) is True:
				datum_cells.add(key)
			else:
				has_independent_cell = True

	# A scope whose only opening is the operator's datum has nothing independent left to sample,
	# so the datum is the only opening worth pressing - keep it rather than refuse to probe, and
	# read the agreement as partly self-referential. A scanned cell that is neither a datum nor
	# DISABLED on the side still counts as the independent opening that holds the datum out.
	exclude_datums = has_independent_cell and (
		scanned_cells is None
		or any(key not in datum_cells and key not in disabled_cells for key in scanned_cells)
	)

	if exclude_datums is True:
		excluded |= datum_cells

	return excluded


def _spread_shelf_indices(shelf_count: int, wanted: int) -> list[int]:
	"""Evenly spaced shelf positions, so the sample set covers the module rather than one shelf."""
	if shelf_count <= 0:
		return []

	if wanted >= shelf_count:
		return list(range(shelf_count))

	if wanted == 1:
		return [shelf_count // 2]

	return sorted({round(index * (shelf_count - 1) / (wanted - 1)) for index in range(wanted)})


def _probe_cell_for_shelf(
	cells: list[tuple[ScanDetectedCell, float]],
	*,
	target_x_mm: float | None,
) -> tuple[ScanDetectedCell, float] | None:
	"""One cell per shelf: nearest to the target x, or the shelf's own middle."""
	if len(cells) == 0:
		return None

	if target_x_mm is None:
		return cells[len(cells) // 2]

	return min(cells, key=lambda pair: abs(float(pair[0].center_x_mm) - target_x_mm))


def select_z_stall_probes(
	analysis: Any,
	wall_depths: dict[tuple[int, int], float],
	max_probe_tool_x_mm: float | None = None,
	min_probe_tool_x_mm: float | None = None,
	excluded_cells: set[tuple[int, int]] | None = None,
	shelves_to_probe: int = 3,
) -> list[tuple[ScanDetectedCell, float]]:
	"""Pick one opening on each of a few shelves, spread across the module.

	The samples are comparable across shelves because each one subtracts its own cell's wall depth,
	so a shelf whose plate reads deeper is normalised before it enters the fit. Sampling several
	shelves therefore does two things a single shelf cannot: it validates the model where it will be
	used (every selected shelf's own residual is visible), and it makes the intercept a median over
	shelves rather than three points that share one shelf's failure mode.

	Each shelf contributes one opening, chosen so the set spans X (the outer shelves take the module's
	extremes) because the tilt still has to be refutable; the remaining shelves follow as reserves,
	used only when a point is dropped.
	"""
	shelves = [shelf for shelf in analysis.shelves if len(shelf.cells) > 0]

	if len(shelves) == 0:
		return []

	excluded = set() if excluded_cells is None else set(excluded_cells)
	ordered_shelves = sorted(shelves, key=lambda shelf: float(shelf.center_y_mm))
	valid_by_shelf: list[list[tuple[ScanDetectedCell, float]]] = []

	for shelf in ordered_shelves:
		valid_by_shelf.append(
			[
				(cell, float(wall_depths[(int(cell.shelf_idx), int(cell.cell_idx))]))
				for cell in sorted(shelf.cells, key=lambda cell: float(cell.center_x_mm))
				if (int(cell.shelf_idx), int(cell.cell_idx)) not in excluded
				and float(wall_depths.get((int(cell.shelf_idx), int(cell.cell_idx)), 0.0)) > 0.0
				and is_probe_reachable(cell, max_probe_tool_x_mm, min_probe_tool_x_mm)
			]
		)

	available = [index for index, cells in enumerate(valid_by_shelf) if len(cells) > 0]

	if len(available) == 0:
		return []

	# X targets spread the sampled openings over the module, which keeps a tilt refutable.
	usable_xs = [float(cell.center_x_mm) for index in available for cell, _ in valid_by_shelf[index]]
	min_x_mm, max_x_mm = min(usable_xs), max(usable_xs)
	primary_positions = _spread_shelf_indices(len(ordered_shelves), max(1, shelves_to_probe))
	x_targets = {
		position: (
			min_x_mm
			if rank == 0
			else max_x_mm
			if rank == len(primary_positions) - 1 and len(primary_positions) >= 2
			else (min_x_mm + max_x_mm) / 2.0
		)
		for rank, position in enumerate(primary_positions)
	}

	probes: list[tuple[ScanDetectedCell, float]] = []
	seen_cells: set[tuple[int, int]] = set()

	def add_probe(position: int, target_x_mm: float | None) -> None:
		picked = _probe_cell_for_shelf(valid_by_shelf[position], target_x_mm=target_x_mm)

		if picked is None:
			return

		key = (int(picked[0].shelf_idx), int(picked[0].cell_idx))

		if key in seen_cells:
			return

		seen_cells.add(key)
		probes.append(picked)

	for position in primary_positions:
		add_probe(position, x_targets.get(position))

	for position in available:
		if position in primary_positions:
			continue

		add_probe(position, None)

	return probes


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
	loaded_candidates: list[Any] = list(candidates or [])

	# A grid-built analysis has no run metadata: its wall depths arrive with the analysis.
	if len(loaded_candidates) == 0 and getattr(analysis, "metadata_path", None) is not None:
		metadata = load_json_file(Path(str(analysis.metadata_path)))
		loaded_candidates = load_candidates_from_metadata(metadata)

	probe_wall_depths = wall_depths_by_cell(analysis=analysis, candidates=loaded_candidates)

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
		probe_wall_depths,
		max_probe_tool_x_mm,
		min_probe_tool_x_mm,
		excluded_cells=excluded_cells,
		shelves_to_probe=int(getattr(scan_config, "z_stall_probe_shelves", 3)),
	)
	if len(probes) == 0:
		raise ValueError(
			"z stall anchor: no reachable detected cell with a wall depth to probe "
			"(disabled and hand-calibrated cells are excluded)"
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
	probe_repeats = int(getattr(scan_config, "z_stall_probe_repeats", 2))
	repeat_tolerance_mm = float(getattr(scan_config, "z_stall_probe_repeat_tolerance_mm", 1.0))
	print_status(
		f"z_stall_anchor=start side={side_name} rotation_deg={rotation_center_deg:.3f} "
		f"stage_extension_mm={stage_extension_mm:.2f} candidate_cells={len(probes)} "
		f"excluded_cells={0 if excluded_cells is None else len(excluded_cells)} "
		f"probe_target_mm={probe_target_mm:.1f} repeats={probe_repeats} z_axis_limit_mm="
		f"{'unknown' if z_axis_limit_mm is None else f'{z_axis_limit_mm:.1f}'}"
	)

	# When the probe target IS the axis ceiling there is no travel to spare, so a probe that runs
	# out of travel is indistinguishable from one that found no wall: both come back reason=limit.
	# Say so up front, otherwise a rack that has drifted out of reach reads as a rack with no walls.
	z_axis_limit_value_mm = None if z_axis_limit_mm is None else float(z_axis_limit_mm)
	has_no_z_headroom = z_axis_limit_value_mm is not None and probe_target_mm >= z_axis_limit_value_mm

	if has_no_z_headroom is True:
		print_status(
			f"z_stall_anchor=no_z_headroom probe_target_mm={probe_target_mm:.1f} "
			f"z_axis_limit_mm={0.0 if z_axis_limit_value_mm is None else z_axis_limit_value_mm:.1f} "
			"reason=limit cannot distinguish 'no wall' from 'wall beyond Z travel'"
		)

	# Probes are ordered requested shelves first, then reserves, so the run has the requested
	# coverage once that many distinct shelves are in the sample set.
	target_shelf_count = min(
		int(getattr(scan_config, "z_stall_probe_shelves", 3)),
		len({int(cell.shelf_idx) for cell, _ in probes}),
	)

	samples: list[ZStallSample] = []
	sampled_shelves: set[int] = set()
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

				if len(contacts) > 0 and len(contacts) < max(1, probe_repeats):
					# A lone contact is not evidence of a wall: the first attempt stalled and the
					# second found nothing, so either the plate moved or the contact was not the plate.
					missed_repeat = (
						f"{cell_key[0]}:{cell_key[1]} stalled at {contacts[0]:.3f}mm then did not "
						f"stall again ({reason})"
					)
					dropped_repeats.append(missed_repeat)
					print_status(f"z_stall_anchor=unstable probe={missed_repeat}")

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

		if len(contacts) < max(1, probe_repeats):
			# Only the full repeat set tells a wall from a rack that moved between contacts, so a
			# point that lost a contact (a miss, or a second attempt rejected as too shallow)
			# is dropped rather than sampled on its single reading.
			skipped.append(
				f"{cell_key[0]}:{cell_key[1]} ({probe_reason}, {len(contacts)} of "
				f"{max(1, probe_repeats)} contacts)"
			)
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
		sampled_shelves.add(cell_key[0])
		print_status(
			f"z_stall_anchor=sample probe={cell_key[0]}:{cell_key[1]} "
			f"x_mm={probe_x_mm:.3f} z_contact_mm={z_contact_mm:.3f} z_place_mm={z_place_mm:.3f} "
			f"tray_mm={tray_engagement_mm:.3f} wall_depth_mm={float(probe_wall_depth_mm):.3f} "
			f"anchor_z_offset_mm={z_offset_anchor_mm:.3f} delta_mm={delta_mm:.3f}"
		)

		# The requested shelves come first and a reserve only replaces a shelf that did not yield a
		# sample, so stop once the run has that many distinct shelves in the sample set.
		if len(sampled_shelves) >= target_shelf_count:
			print_status(
				f"z_stall_anchor=stop shelves_sampled={len(sampled_shelves)} "
				f"target_shelves={target_shelf_count} samples={len(samples)}"
			)
			break

	if len(samples) == 0:
		if len(dropped_repeats) > 0:
			raise ValueError(
				"z stall anchor: every probe disagreed with itself on the second attempt (a different "
				"depth or no contact at all), so the rack is moving under probe force rather than "
				"holding still: "
				f"{'; '.join(dropped_repeats)}"
			)

		if limit_miss_count == len(probes) and has_no_z_headroom is True:
			raise ValueError(
				f"z stall anchor: all {len(probes)} probes ran the Z axis to its travel limit "
				f"({0.0 if z_axis_limit_value_mm is None else z_axis_limit_value_mm:.1f}mm) without contact. The probe target equals that limit, "
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
		# Fit in the frame the model is applied in: the probe aims at probe_x (the opening plus the
		# divider shift), while the stored model is evaluated at the cell's grab x. Fitting on the
		# aimed point would tilt the model by the shift's worth of X.
		intercept_mm, slope_per_mm = resolve_z_delta_model(
			[(sample.probe_x_mm - probe_x_shift_mm, sample.delta_mm) for sample in samples]
		)
		if slope_per_mm == 0.0:
			fit_source = "constant"
		elif len(samples) >= 3:
			fit_source = "tilt"
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
		fit_centroid_x_mm=sum(
			float(sample.probe_x_mm) - probe_x_shift_mm for sample in samples
		) / len(samples),
		samples=samples,
		skipped=skipped,
		dropped_repeats=dropped_repeats,
	)


def resolve_z_stall_model(
	*,
	result: ZStallResult,
	scan_config: Any,
	side: StorageModuleSide,
) -> ZStallDecision:
	"""The (intercept, slope) a probed fit installs with, in the grab frame the model is read in.

	`ZStallResult` reports the fit as measured: when the probe could not span enough X to see a
	tilt, that fit is tilt-free by construction and its intercept is the sampled deltas' value at
	the samples' centroid, not at x = 0. Substituting the configured tilt for the missing one
	therefore requires re-basing the intercept with `-slope * centroid_x`, otherwise the installed
	line sits `slope * centroid_x` mm off at every cell - deep enough to matter on a long module.

	A tilt the probe did measure always wins over the configured one, and its intercept is already
	the x = 0 value, so it is installed untouched. The same decision is what the manual-datum gate
	judges and what the standalone probe script reports, so all three agree.
	"""
	if float(result.slope_per_mm) != 0.0:
		return ZStallDecision(
			intercept_mm=float(result.intercept_mm),
			slope_per_mm=float(result.slope_per_mm),
			source="probe",
		)

	configured_model = get_scan_z_offset_model(scan_config=scan_config, side=side)

	if configured_model is None or float(configured_model.slope_per_mm) == 0.0:
		return ZStallDecision(
			intercept_mm=float(result.intercept_mm), slope_per_mm=0.0, source="probe_constant"
		)

	if float(result.x_span_mm) >= Z_DELTA_TILT_MIN_X_SPAN_MM:
		return ZStallDecision(
			intercept_mm=float(result.intercept_mm), slope_per_mm=0.0, source="probe_constant"
		)

	slope_per_mm = float(configured_model.slope_per_mm)
	centroid_x_mm = float(result.fit_centroid_x_mm)
	print_status(
		f"z_stall_anchor=slope_from_config side={side.value} x_span_mm={float(result.x_span_mm):.1f} "
		f"slope_per_mm={slope_per_mm:.5f} centroid_x_mm={centroid_x_mm:.1f} "
		f"(probe span below {Z_DELTA_TILT_MIN_X_SPAN_MM:.0f}mm, no tilt measured)"
	)

	return ZStallDecision(
		intercept_mm=float(result.intercept_mm) - slope_per_mm * centroid_x_mm,
		slope_per_mm=slope_per_mm,
		source="config_short_span",
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


def build_probe_analysis_from_grid(
	*,
	grid: Any,
	side: StorageModuleSide,
	scan_config: Any,
	wall_depths: dict[tuple[int, int], float],
	shelf_idx: int | None = None,
	cells: set[tuple[int, int]] | None = None,
) -> Any:
	"""Probe targets taken from the stored calibration points rather than a saved scan.

	For a rack that has moved since the last scan the stored points are the best-known positions
	of its openings, because they are what the operator dialled in by hand. They are grab points,
	so they are converted back into the detection frame the probe aims from
	(`tool = grab - xy_offset`), and the wall depth still comes from a sweep, since the probe
	subtracts it: a cell without one contributes no delta and is skipped.
	"""
	xy_offset = get_scan_xy_offset(scan_config=scan_config, side=side)
	offset_x_mm = 0.0 if xy_offset is None else float(xy_offset[0])
	offset_y_mm = 0.0 if xy_offset is None else float(xy_offset[1])
	laser_offset_x_mm = float(getattr(scan_config, "laser_offset_x_mm", 0.0) or 0.0)
	laser_offset_y_mm = float(getattr(scan_config, "laser_offset_y_mm", 0.0) or 0.0)
	hole_width_mm = float(getattr(scan_config, "expected_hole_width_mm", 26.0))
	hole_height_mm = float(getattr(scan_config, "expected_hole_height_mm", 30.0))

	cells_by_shelf: dict[int, list[ScanDetectedCell]] = {}

	for shelf in grid.get_side(side):
		if shelf_idx is not None and int(shelf.shelf_idx) != int(shelf_idx):
			continue

		for cell in shelf.cells:
			key = (int(shelf.shelf_idx), int(cell.cell_idx))
			point = cell.calibration_point

			if point is None or (cells is not None and key not in cells):
				continue

			tool_x_mm = float(point.x) - offset_x_mm
			tool_y_mm = float(point.y) - offset_y_mm
			cells_by_shelf.setdefault(key[0], []).append(
				ScanDetectedCell(
					shelf_idx=key[0],
					cell_idx=key[1],
					center_x_mm=tool_x_mm - laser_offset_x_mm,
					center_y_mm=tool_y_mm + laser_offset_y_mm,
					tool_x_mm=tool_x_mm,
					tool_y_mm=tool_y_mm,
					width_mm=hole_width_mm,
					height_mm=hole_height_mm,
					point_count=0,
					bbox_x_min=tool_x_mm - laser_offset_x_mm - (hole_width_mm / 2.0),
					bbox_x_max=tool_x_mm - laser_offset_x_mm + (hole_width_mm / 2.0),
					bbox_y_min=tool_y_mm - (hole_height_mm / 2.0),
					bbox_y_max=tool_y_mm + (hole_height_mm / 2.0),
				)
			)

	shelves = [
		ScanDetectedShelf(
			shelf_idx=shelf_index,
			center_y_mm=float(median([cell.tool_y_mm for cell in shelf_cells])),
			shelf_height_mm=max(float(cell.height_mm) for cell in shelf_cells),
			shelf_width_mm=sum(float(cell.width_mm) for cell in shelf_cells),
			cells=sorted(shelf_cells, key=lambda cell: int(cell.cell_idx)),
		)
		for shelf_index, shelf_cells in sorted(cells_by_shelf.items())
	]

	return SimpleNamespace(
		shelves=shelves,
		metadata_path=None,
		wall_depths_by_cell=dict(wall_depths),
	)
