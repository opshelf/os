"""Derive the laser-to-gripper reference offset from hand-calibrated cells.

A manually calibrated cell carries the grab point an operator dialled in by hand; the scan
measures the same opening in the laser frame. The difference between the two is the per-side
offset the apply step needs, so a machine can replace the rough offset it was configured
with (copied from another machine or guessed) by the value its own reference cells imply.

The bottom-left cell of each side (shelf 0, cell 0) is the anchor: it is the one opening every
machine hand-calibrates first, and `select_reference_datum_cell` is what a run checks before it
starts, so a run that cannot measure that cell fails instead of falling back to the configured
offset. Other manual cells on the side still join the derivation as extra samples.

The stored point is `detected + xy_offset`, so the derivation is `manual - detected`, taken as
a median across the reference cells and reported with its spread: one hand-set cell is only
as precise as the operator's jogging, while several cells also expose a mistyped or stale
datum.

Matching a manual point to a detected opening predicts where the scan should have seen it
(`manual - configured_offset`) and then takes the nearest detected opening inside a narrow
window. Predicting first matters: the offset itself can be tens of millimetres (the right
module's is near -100), which is larger than the spacing between openings, so matching by
raw proximity would pick a neighbouring cell and shift the whole module by one pitch. That is
why a side with no configured offset is refused outright: predicting from zero is raw
proximity in disguise, and it silently lands on a neighbouring opening a full pitch away.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from statistics import median
from typing import Any

from openshelf_os.storage import Cell, StorageGrid, StorageModuleSide
from .core import get_gripper_xy_correction, get_wall_depth_to_gripper_z_model, z_offset_at_stage
from .recording import load_candidates_from_metadata, load_json_file
from .scan import ScanDetectedCell
from .staged_scan import Z_DELTA_MAX_ABS_MM, collect_detected_cells, wall_depths_by_cell

logger = logging.getLogger(__name__)

# How far the predicted position may sit from a detected opening. The prediction carries the
# configured offset's error, so this must exceed a few millimetres of drift, while staying
# well under half the smallest opening pitch (about 52mm on a 105mm rack): that is what keeps
# a match unambiguous even when the derived offset is tens of millimetres.
REFERENCE_MATCH_WINDOW_MM = 25.0

# Two hand-set cells that disagree by more than this cannot share one fixed tool geometry, so
# the derivation refuses rather than averaging a bad datum into every cell.
REFERENCE_MAX_SPREAD_MM = 1.5

# How far a stamped z may be carried to the live stage extension. The carry is exact for the
# sub-millimetre drift an extension recalibration causes. A larger gap means the stamp no longer
# describes the z that works: lb4a-3's datum kept a 47.875mm stamp after the shelf extension moved
# to 46mm and its z was re-validated there, and carrying it put every derived cell 1.9mm too deep.
# Refused, the operator re-saves the datum's z at the current extension instead.
REFERENCE_Z_MAX_STAGE_CARRY_MM = 1.0

# The bottom-left opening of each side is where the reference frame is anchored: an operator
# jogs the gripper onto that one cell by hand and every derived value is measured from it, so a
# run that cannot use it has nothing to frame against and must not invent one.
REFERENCE_DATUM_SHELF_IDX = 0
REFERENCE_DATUM_CELL_IDX = 0


@dataclass
class ReferenceOffsetSample:
	shelf_idx: int
	cell_idx: int
	manual_x_mm: float
	manual_y_mm: float
	detected_x_mm: float
	detected_y_mm: float
	delta_x_mm: float
	delta_y_mm: float
	wall_depth_mm: float | None = None
	derived_intercept_mm: float | None = None
	z_captured_stage_mm: float | None = None


@dataclass
class ReferenceOffsetResult:
	side: StorageModuleSide
	offset_x_mm: float
	offset_y_mm: float
	spread_x_mm: float
	spread_y_mm: float
	samples: list[ReferenceOffsetSample] = field(default_factory=list)
	intercept_mm: float | None = None
	slope_per_mm: float = 0.0
	configured_offset_x_mm: float | None = None
	configured_offset_y_mm: float | None = None
	z_captured_stage_mm: float | None = None
	z_captured_stage_samples: int = 0
	# The stage extension every stamped sample's intercept was carried to; None when no live stage
	# was given, in which case the intercepts are as captured.
	z_stage_extension_mm: float | None = None
	skipped: list[str] = field(default_factory=list)

	@property
	def sample_count(self) -> int:
		return len(self.samples)

	@property
	def z_capture_conditions_missing(self) -> int:
		"""Samples saved before the capture stage was recorded: their z cannot be carried to the live stage."""
		return self.sample_count - self.z_captured_stage_samples

	@property
	def used_configured_offset(self) -> bool:
		return self.configured_offset_x_mm is not None and self.configured_offset_y_mm is not None

	def described_delta_mm(self) -> tuple[float | None, float | None]:
		"""How far this derivation moves the configured offset, when there was one."""
		if self.used_configured_offset is False:
			return None, None

		configured_x_mm = self.configured_offset_x_mm
		configured_y_mm = self.configured_offset_y_mm

		if configured_x_mm is None or configured_y_mm is None:
			return None, None

		return (
			self.offset_x_mm - float(configured_x_mm),
			self.offset_y_mm - float(configured_y_mm),
		)


def check_datum_stage_carry(result: ReferenceOffsetResult | None) -> None:
	"""Refuse a stamped datum captured too far from the live stage extension to be carried.

	The carry is exact for the small drift an extension recalibration causes; a z saved with the
	stage retracted is not a shelf-extension datum at all and would shift every derived cell.
	"""
	if result is None or result.z_stage_extension_mm is None:
		return

	live_stage_mm = float(result.z_stage_extension_mm)

	for sample in result.samples:
		if sample.derived_intercept_mm is None or sample.z_captured_stage_mm is None:
			continue

		_require_stage_carry(
			side=result.side,
			shelf_idx=sample.shelf_idx,
			cell_idx=sample.cell_idx,
			captured_stage_mm=float(sample.z_captured_stage_mm),
			live_stage_mm=live_stage_mm,
		)


def check_stamped_reference_carry(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	stage_extension_mm: float | None,
	shelf_idx: int | None = None,
	cell_idx: int | None = None,
) -> bool:
	"""Whether the side has a hand-calibrated cell whose z the apply can carry to the live stage.

	The start-time counterpart of `check_datum_stage_carry`: a stamp too far from the live stage
	is refused here, before the scan, rather than by the apply after it. Without a live stage the
	carry cannot be judged, so any stamp counts. `shelf_idx`/`cell_idx` limit the check to the
	cells a narrow rescan derives from, as `select_datum_derivation_scope` returns them.
	"""
	has_stamp = False

	for cell_shelf_idx, cell in select_manual_reference_cells(
		grid=grid, side=side, shelf_idx=shelf_idx, cell_idx=cell_idx
	):
		captured_stage_mm = cell.calibration_point.z_captured_at_stage_mm

		if captured_stage_mm is None:
			continue

		has_stamp = True

		if stage_extension_mm is not None:
			_require_stage_carry(
				side=side,
				shelf_idx=cell_shelf_idx,
				cell_idx=int(cell.cell_idx),
				captured_stage_mm=float(captured_stage_mm),
				live_stage_mm=float(stage_extension_mm),
			)

	return has_stamp


def _require_stage_carry(
	*,
	side: StorageModuleSide,
	shelf_idx: int,
	cell_idx: int,
	captured_stage_mm: float,
	live_stage_mm: float,
) -> None:
	if abs(captured_stage_mm - live_stage_mm) > REFERENCE_Z_MAX_STAGE_CARRY_MM:
		raise ValueError(
			f"reference z: the {side.value} reference cell {shelf_idx}:{cell_idx} "
			f"had its z saved at stage extension {captured_stage_mm:.2f}mm, but the live stage "
			f"extension is {live_stage_mm:.2f}mm (more than {REFERENCE_Z_MAX_STAGE_CARRY_MM:.1f}mm "
			"apart, so it cannot be carried); in cell calibration extend the stage to the shelf "
			"extension, jog that cell's z onto the tray (the stage is recorded only when the z "
			"changes) and save it"
		)


def stamped_datum_intercept_mm(result: ReferenceOffsetResult | None) -> float | None:
	"""The z intercept the hand-calibrated cells imply at the live stage, or None without one.

	Only samples whose capture stage is known count: an unstamped z offset may have been jogged
	at any stage extension, and every millimetre of that is a millimetre on every derived cell.
	Stamped samples captured too far from the live stage, that still disagree beyond one tool
	geometry, or that imply an implausible intercept are refused.
	"""
	if result is None or result.z_stage_extension_mm is None:
		return None

	check_datum_stage_carry(result)

	intercepts = [
		float(sample.derived_intercept_mm)
		for sample in result.samples
		if sample.derived_intercept_mm is not None and sample.z_captured_stage_mm is not None
	]

	if len(intercepts) == 0:
		return None

	spread_mm = max(intercepts) - min(intercepts)

	if spread_mm > REFERENCE_MAX_SPREAD_MM:
		raise ValueError(
			f"reference z: the manual reference cells on the {result.side.value} imply z intercepts "
			f"{spread_mm:.2f}mm apart at stage extension {float(result.z_stage_extension_mm):.2f}mm, above the "
			f"{REFERENCE_MAX_SPREAD_MM:.2f}mm one fixed tool geometry can explain; in cell calibration "
			"extend the stage to the shelf extension, jog each stale reference cell's z onto the tray "
			"(the stage is recorded only when the z changes) and save it, or clear 'keep manual' on "
			"the stale ones"
		)

	intercept_mm = float(median(intercepts))

	if abs(intercept_mm) > Z_DELTA_MAX_ABS_MM:
		raise ValueError(
			f"reference z: the manual reference cells on the {result.side.value} imply an implausible z "
			f"intercept {intercept_mm:.1f}mm (limit +/-{Z_DELTA_MAX_ABS_MM:.0f}mm); refusing to apply it "
			"to every cell; in cell calibration extend the stage to the shelf extension, jog the "
			"reference cells' z onto the tray (the stage is recorded only when the z changes) and "
			"save them"
		)

	return intercept_mm


def z_model_datum_agreement_mm(
	*,
	intercept_mm: float,
	slope_per_mm: float,
	configured_slope_per_mm: float,
	manual_reference: ReferenceOffsetResult | None,
) -> float | None:
	"""Worst-case residual, in mm, between a probed z model and the hand-calibrated datum.

	"Agrees" means where the two models put the datum cells: `intercept + slope * grab_x`
	evaluated at each reference sample. The datum's intercept was derived with the configured
	slope, so comparing bare intercepts would let a probe-fitted tilt move the installed model
	away from the datum cells unchecked.

	Returns `None` when there is no datum or no datum intercept, and falls back to the bare
	intercept difference when the datum has no usable samples.
	"""
	if manual_reference is None or manual_reference.intercept_mm is None:
		return None

	if len(manual_reference.samples) == 0:
		return abs(float(intercept_mm) - float(manual_reference.intercept_mm))

	residuals = [
		abs(
			(float(intercept_mm) + float(slope_per_mm) * float(sample.manual_x_mm))
			- (
				float(sample.derived_intercept_mm)
				+ float(configured_slope_per_mm) * float(sample.manual_x_mm)
			)
		)
		for sample in manual_reference.samples
		if sample.derived_intercept_mm is not None
	]

	if len(residuals) == 0:
		return abs(float(intercept_mm) - float(manual_reference.intercept_mm))

	return max(residuals)


def select_reference_datum_cell(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
) -> tuple[int, Cell] | None:
	"""The side's bottom-left hand-calibrated cell, or None when it is not a usable datum.

	Missing (no shelf 0 / no cell 0), uncalibrated, or not marked manual all answer None: every
	one of them leaves the run without the reference the derivation predicts its matches from.
	"""
	for shelf in grid.get_side(side):
		if int(shelf.shelf_idx) != REFERENCE_DATUM_SHELF_IDX:
			continue

		for cell in shelf.cells:
			if int(cell.cell_idx) != REFERENCE_DATUM_CELL_IDX:
				continue

			if cell.is_manual is not True or cell.calibration_point is None:
				return None

			return (int(shelf.shelf_idx), cell)

	return None


def select_datum_derivation_scope(
	*,
	mode: str | None,
	shelf_idx: int | None = None,
	cell_idx: int | None = None,
) -> tuple[int | None, int | None] | None:
	"""Shelf and cell the datum is derived from in this run's scan, or None when it cannot be seen.

	A full-module scan sees every opening, so it frames the side from all of its manual cells
	(None, None). A shelf or cell rescan sees only its own part of the side: when that part *is*
	the datum the run re-frames from it, and otherwise the scan holds nothing to measure the frame
	against, so the run keeps the frame the datum already established rather than failing a
	routine rescan.
	"""
	if mode not in ("shelf", "cell"):
		return (None, None)

	# Both indices are required by the scope runner before a narrow scope runs; a missing one
	# cannot be the datum, so the derivation stays out of the way rather than raising here.
	if shelf_idx is None or int(shelf_idx) != REFERENCE_DATUM_SHELF_IDX:
		return None

	if mode == "shelf":
		return (int(shelf_idx), None)

	if cell_idx is None or int(cell_idx) != REFERENCE_DATUM_CELL_IDX:
		return None

	return (int(shelf_idx), int(cell_idx))


def select_manual_reference_cells(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	shelf_idx: int | None = None,
	cell_idx: int | None = None,
) -> list[tuple[int, Cell]]:
	"""Hand-calibrated cells that can act as the reference frame, in scope order."""
	selected: list[tuple[int, Cell]] = []

	for shelf in grid.get_side(side):
		if shelf_idx is not None and int(shelf.shelf_idx) != int(shelf_idx):
			continue

		for cell in shelf.cells:
			if cell.is_manual is not True or cell.calibration_point is None:
				continue

			if cell_idx is not None and int(cell.cell_idx) != int(cell_idx):
				continue

			selected.append((int(shelf.shelf_idx), cell))

	return selected


def _load_candidates(analysis: Any) -> list[Any]:
	"""Sweep candidates from the run metadata, when the run recorded any.

	Only the reported z intercept needs them: the x/y offset still derives without a
	readable metadata file, so a missing one is a warning rather than a failure.
	"""
	metadata_path = getattr(analysis, "metadata_path", None)

	if metadata_path is None:
		return []

	try:
		return load_candidates_from_metadata(load_json_file(metadata_path))
	except Exception as error:
		logger.warning(
			"reference_offset_candidates_unavailable",
			extra={"metadata_path": str(metadata_path), "error": str(error)},
		)
		return []


def _wall_depths_by_cell(
	*,
	analysis: Any,
	candidates: list[Any],
) -> dict[tuple[int, int], float]:
	return wall_depths_by_cell(analysis=analysis, candidates=candidates)


def _match_detected_cell(
	*,
	predicted_x_mm: float,
	predicted_y_mm: float,
	detected_cells: list[ScanDetectedCell],
	used_keys: set[tuple[int, int]],
) -> ScanDetectedCell | None:
	matched_cell: ScanDetectedCell | None = None
	matched_distance_mm: float | None = None

	for detected_cell in detected_cells:
		key = (int(detected_cell.shelf_idx), int(detected_cell.cell_idx))

		if key in used_keys:
			continue

		distance_mm = math.hypot(
			float(detected_cell.tool_x_mm) - predicted_x_mm,
			float(detected_cell.tool_y_mm) - predicted_y_mm,
		)

		if distance_mm > REFERENCE_MATCH_WINDOW_MM:
			continue

		if matched_distance_mm is not None and distance_mm >= matched_distance_mm:
			continue

		matched_cell = detected_cell
		matched_distance_mm = distance_mm

	return matched_cell


def derive_reference_offsets(
	*,
	grid: StorageGrid,
	analysis: Any,
	side: StorageModuleSide,
	scan_config: Any,
	shelf_idx: int | None = None,
	cell_idx: int | None = None,
	candidates: list[Any] | None = None,
	stage_extension_mm: float | None = None,
) -> ReferenceOffsetResult:
	"""Laser-to-gripper offset implied by the hand-calibrated cells in scope.

	The run-start guard is what makes the bottom-left datum a prerequisite; this reads whatever
	manual cells the scope holds, so a full-side run medians the datum with any other hand-set
	cell on the side. Raises ValueError when the reference frame cannot be trusted: no manual
	cell in scope, no configured offset to predict the manual cells from, no manual point matched
	a detected opening, or the reference cells disagree by more than a fixed tool geometry can
	explain. Falling back to the configured offset would calibrate the module against the very
	value the operator asked to replace, so the caller is told why.

	With `stage_extension_mm`, each stamped sample's z offset is carried from the stage it was
	captured at to that one before its intercept is taken, so datums jogged at different stage
	extensions describe the same tool.
	"""
	if candidates is None:
		candidates = _load_candidates(analysis)

	reference_cells = select_manual_reference_cells(
		grid=grid,
		side=side,
		shelf_idx=shelf_idx,
		cell_idx=cell_idx,
	)

	if len(reference_cells) == 0:
		raise ValueError(
			f"reference offsets: the {side.value} has no cell marked manual in scope to frame the "
			"run from; tick 'keep manual' when saving the bottom-left reference cell"
		)

	configured_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)
	configured_offset_x_mm = None if configured_offset is None else float(configured_offset[0])
	configured_offset_y_mm = None if configured_offset is None else float(configured_offset[1])

	if configured_offset_x_mm is None or configured_offset_y_mm is None:
		# With nothing configured there is no prediction to make, and a proximity match at a
		# ~105mm pitch silently lands on a neighbouring opening: one wrong match re-frames and
		# persists every cell on the module.
		side_name = "front" if side == StorageModuleSide.FRONT else "back"
		raise ValueError(
			f"reference offsets on the {side.value}: no "
			f"robot.grid.scan.{side_name}_gripper_xy_correction_mm is configured, so there is nothing to "
			"predict the manual cells' detected positions from; set a rough one first (it only "
			"needs to land within a few tens of millimetres of the truth), then re-run; the "
			"derivation replaces it with the value the reference cells imply"
		)

	predictor_x_mm = configured_offset_x_mm
	predictor_y_mm = configured_offset_y_mm

	detected_cells = collect_detected_cells(analysis)
	wall_depths = _wall_depths_by_cell(analysis=analysis, candidates=candidates)
	configured_model = get_wall_depth_to_gripper_z_model(scan_config=scan_config, side=side)
	slope_per_mm = 0.0 if configured_model is None else float(configured_model.x_slope_mm_per_mm)

	samples: list[ReferenceOffsetSample] = []
	skipped: list[str] = []
	used_keys: set[tuple[int, int]] = set()

	for shelf_id, manual_cell in reference_cells:
		point = manual_cell.calibration_point

		if point is None:
			skipped.append(
				f"{side.value} {shelf_id}:{int(manual_cell.cell_idx)}: no calibration point stored"
			)
			continue

		manual_x_mm = float(point.x)
		manual_y_mm = float(point.y)
		predicted_x_mm = manual_x_mm - predictor_x_mm
		predicted_y_mm = manual_y_mm - predictor_y_mm
		matched_cell = _match_detected_cell(
			predicted_x_mm=predicted_x_mm,
			predicted_y_mm=predicted_y_mm,
			detected_cells=detected_cells,
			used_keys=used_keys,
		)

		if matched_cell is None:
			skipped.append(
				f"{side.value} {shelf_id}:{int(manual_cell.cell_idx)}: no detected opening within "
				f"{REFERENCE_MATCH_WINDOW_MM:.0f}mm of its predicted position "
				f"({predicted_x_mm:.1f}, {predicted_y_mm:.1f})"
			)
			continue

		used_keys.add((int(matched_cell.shelf_idx), int(matched_cell.cell_idx)))
		wall_depth_mm = wall_depths.get((int(matched_cell.shelf_idx), int(matched_cell.cell_idx)))
		captured_stage_mm = getattr(point, "z_captured_at_stage_mm", None)
		z_offset_mm = float(point.z_offset)

		if captured_stage_mm is not None and stage_extension_mm is not None:
			z_offset_mm = z_offset_at_stage(
				z_offset_mm=z_offset_mm,
				captured_stage_mm=float(captured_stage_mm),
				stage_extension_mm=float(stage_extension_mm),
			)

		derived_intercept_mm = (
			None
			if wall_depth_mm is None
			else z_offset_mm - float(wall_depth_mm) - slope_per_mm * manual_x_mm
		)

		samples.append(
			ReferenceOffsetSample(
				shelf_idx=shelf_id,
				cell_idx=int(manual_cell.cell_idx),
				manual_x_mm=manual_x_mm,
				manual_y_mm=manual_y_mm,
				detected_x_mm=float(matched_cell.tool_x_mm),
				detected_y_mm=float(matched_cell.tool_y_mm),
				delta_x_mm=manual_x_mm - float(matched_cell.tool_x_mm),
				delta_y_mm=manual_y_mm - float(matched_cell.tool_y_mm),
				wall_depth_mm=wall_depth_mm,
				derived_intercept_mm=derived_intercept_mm,
				z_captured_stage_mm=None if captured_stage_mm is None else float(captured_stage_mm),
			)
		)

	if len(samples) == 0:
		raise ValueError(
			"reference offsets: no manual reference cell matched a detected "
			f"opening on the {side.value}: {'; '.join(skipped)}"
		)

	if len(samples) == 1:
		# One datum is only as precise as the operator's jogging, so say so rather than let a
		# single-cell derivation read as a measurement of the tool geometry.
		logger.warning(
			"reference_offset_single_sample",
			extra={
				"side": side.value,
				"shelf_idx": samples[0].shelf_idx,
				"cell_idx": samples[0].cell_idx,
			},
		)

	offset_x_mm = float(median([sample.delta_x_mm for sample in samples]))
	offset_y_mm = float(median([sample.delta_y_mm for sample in samples]))
	spread_x_mm = max(sample.delta_x_mm for sample in samples) - min(
		sample.delta_x_mm for sample in samples
	)
	spread_y_mm = max(sample.delta_y_mm for sample in samples) - min(
		sample.delta_y_mm for sample in samples
	)

	if spread_x_mm > REFERENCE_MAX_SPREAD_MM or spread_y_mm > REFERENCE_MAX_SPREAD_MM:
		raise ValueError(
			f"reference offsets: the manual reference cells on the {side.value} "
			f"disagree by ({spread_x_mm:.2f}, {spread_y_mm:.2f})mm, above the "
			f"{REFERENCE_MAX_SPREAD_MM:.2f}mm one fixed tool geometry can explain; re-check which "
			"cells are marked manual before calibrating against them"
		)

	intercepts = [
		sample.derived_intercept_mm
		for sample in samples
		if sample.derived_intercept_mm is not None
	]
	captured_stages = [
		sample.z_captured_stage_mm
		for sample in samples
		if sample.z_captured_stage_mm is not None
	]

	return ReferenceOffsetResult(
		side=side,
		offset_x_mm=offset_x_mm,
		offset_y_mm=offset_y_mm,
		spread_x_mm=spread_x_mm,
		spread_y_mm=spread_y_mm,
		samples=samples,
		intercept_mm=None if len(intercepts) == 0 else float(median(intercepts)),
		slope_per_mm=slope_per_mm,
		configured_offset_x_mm=configured_offset_x_mm,
		configured_offset_y_mm=configured_offset_y_mm,
		z_captured_stage_mm=None if len(captured_stages) == 0 else float(median(captured_stages)),
		z_captured_stage_samples=len(captured_stages),
		z_stage_extension_mm=None if stage_extension_mm is None else float(stage_extension_mm),
		skipped=skipped,
	)
