"""Derive the laser-to-gripper reference offset from hand-calibrated cells.

A manually calibrated cell carries the grab point an operator dialled in by hand; the scan
measures the same opening in the laser frame. The difference between the two is the per-side
offset the apply step needs, so a machine can bootstrap its own offsets instead of copying
another machine's numbers or leaving the configured value null.

The stored point is `detected + xy_offset`, so the derivation is `manual - detected`, taken as
a median across the reference cells and reported with its spread: one hand-set cell is only
as precise as the operator's jogging, while several cells also expose a mistyped or stale
datum.

Matching a manual point to a detected opening predicts where the scan should have seen it
(`manual - configured_offset`) and then takes the nearest detected opening inside a narrow
window. Predicting first matters: the offset itself can be tens of millimetres (the right
module's is near -100), which is larger than the spacing between openings, so matching by
raw proximity would pick a neighbouring cell and shift the whole module by one pitch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
from statistics import median
from typing import Any

from openshelf_os.storage import Cell, StorageGrid, StorageModuleSide
from .core import get_scan_xy_offset, get_scan_z_offset_model
from .recording import load_candidates_from_metadata, load_json_file
from .scan import ScanDetectedCell
from .staged_scan import collect_detected_cells, find_candidate_for_detected_cell

logger = logging.getLogger(__name__)

# How far the predicted position may sit from a detected opening. The prediction carries the
# configured offset's error, so this must exceed a few millimetres of drift, while staying
# well under half the smallest opening pitch (about 52mm on a 105mm rack): that is what keeps
# a match unambiguous even when the derived offset is tens of millimetres.
REFERENCE_MATCH_WINDOW_MM = 25.0

# Two hand-set cells that disagree by more than this cannot share one fixed tool geometry, so
# the derivation refuses rather than averaging a bad datum into every cell.
REFERENCE_MAX_SPREAD_MM = 1.5


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
	skipped: list[str] = field(default_factory=list)

	@property
	def sample_count(self) -> int:
		return len(self.samples)

	@property
	def used_configured_offset(self) -> bool:
		return self.configured_offset_x_mm is not None

	def described_delta_mm(self) -> tuple[float | None, float | None]:
		"""How far this derivation moves the configured offset, when there was one."""
		if self.used_configured_offset is False:
			return None, None

		return (
			self.offset_x_mm - float(self.configured_offset_x_mm),
			self.offset_y_mm - float(self.configured_offset_y_mm),
		)


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
	except Exception:
		logger.warning(
			"reference_offset_candidates_unavailable",
			extra={"metadata_path": str(metadata_path)},
			exc_info=True,
		)
		return []


def _wall_depths_by_cell(
	*,
	analysis: Any,
	candidates: list[Any],
) -> dict[tuple[int, int], float]:
	depths: dict[tuple[int, int], float] = {}

	for detected_cell in collect_detected_cells(analysis):
		matched_candidate = find_candidate_for_detected_cell(
			detected_cell=detected_cell,
			candidates=candidates,
		)

		if matched_candidate is None or matched_candidate.wall_depth_mm is None:
			continue

		depths[(int(detected_cell.shelf_idx), int(detected_cell.cell_idx))] = float(
			matched_candidate.wall_depth_mm
		)

	return depths


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
) -> ReferenceOffsetResult:
	"""Laser-to-gripper offset implied by the hand-calibrated cells in scope.

	Raises ValueError when the reference frame cannot be trusted: no manual cell in scope, no
	manual point matched a detected opening, or the reference cells disagree by more than a
	fixed tool geometry can explain. Falling back to the configured offset would calibrate the
	module against the very value the operator asked to replace, so the caller is told why.
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
			f"derive_offsets_from_manual_cells needs at least one cell marked manual in scope on "
			f"the {side.value}; tick 'keep manual' when saving the reference cell"
		)

	configured_offset = get_scan_xy_offset(scan_config=scan_config, side=side)
	configured_offset_x_mm = None if configured_offset is None else float(configured_offset[0])
	configured_offset_y_mm = None if configured_offset is None else float(configured_offset[1])
	predictor_x_mm = 0.0 if configured_offset_x_mm is None else configured_offset_x_mm
	predictor_y_mm = 0.0 if configured_offset_y_mm is None else configured_offset_y_mm

	detected_cells = collect_detected_cells(analysis)
	wall_depths = _wall_depths_by_cell(analysis=analysis, candidates=candidates)
	configured_model = get_scan_z_offset_model(scan_config=scan_config, side=side)
	slope_per_mm = 0.0 if configured_model is None else float(configured_model.slope_per_mm)

	samples: list[ReferenceOffsetSample] = []
	skipped: list[str] = []
	used_keys: set[tuple[int, int]] = set()

	for shelf_id, manual_cell in reference_cells:
		point = manual_cell.calibration_point
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
		derived_intercept_mm = (
			None
			if wall_depth_mm is None
			else float(point.z_offset) - float(wall_depth_mm) - slope_per_mm * manual_x_mm
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
			)
		)

	if len(samples) == 0:
		hint = (
			""
			if configured_offset_x_mm is not None
			else "; no {side}_xy_offset is configured either, so set a rough one first"
		).format(side="left" if side == StorageModuleSide.LEFT else "right")

		raise ValueError(
			"derive_offsets_from_manual_cells matched no manual reference cell to a detected "
			f"opening on the {side.value}: {'; '.join(skipped)}{hint}"
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
			f"derive_offsets_from_manual_cells: the manual reference cells on the {side.value} "
			f"disagree by ({spread_x_mm:.2f}, {spread_y_mm:.2f})mm, above the "
			f"{REFERENCE_MAX_SPREAD_MM:.2f}mm one fixed tool geometry can explain; re-check which "
			"cells are marked manual before calibrating against them"
		)

	intercepts = [
		sample.derived_intercept_mm
		for sample in samples
		if sample.derived_intercept_mm is not None
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
		skipped=skipped,
	)
