from __future__ import annotations

import copy

from datetime import datetime
import logging
from typing import Optional

from pydantic import BaseModel, Field

from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageModuleSide,
)


class ScanBounds(BaseModel):
	x_min: float
	x_max: float
	y_min: float
	y_max: float


class ScanDetectedCell(BaseModel):
	shelf_idx: int
	cell_idx: int
	center_x_mm: float
	center_y_mm: float
	tool_x_mm: float
	tool_y_mm: float
	width_mm: float
	height_mm: float
	point_count: int
	bbox_x_min: float
	bbox_x_max: float
	bbox_y_min: float
	bbox_y_max: float


class ScanDetectedShelf(BaseModel):
	shelf_idx: int
	center_y_mm: float
	shelf_width_mm: float
	cells: list[ScanDetectedCell] = Field(default_factory=list)


class ScanAnalysisResult(BaseModel):
	run_id: str
	side: StorageModuleSide
	bounds: ScanBounds
	step_mm: float
	samples_per_point: int
	settle_ms: int
	wall_depth_mm: float
	wall_depth_tolerance_mm: float
	expected_hole_width_mm: float
	expected_hole_height_mm: float
	min_vertical_wall_coverage_mm: float
	min_horizontal_wall_coverage_mm: float
	total_points: int
	valid_points: int
	raw_scan_path: str
	metadata_path: str
	shelves: list[ScanDetectedShelf] = Field(default_factory=list)
	created_at: datetime



def build_axis_positions(start_mm: float, end_mm: float, step_mm: float) -> list[float]:
	if step_mm <= 0:
		raise ValueError("step_mm must be positive")

	positions: list[float] = []
	direction = 1.0 if end_mm >= start_mm else -1.0
	current = start_mm

	# Stop at the bound: a step past it is a position the axis is clamped back from, so it would
	# duplicate the end bin and leave the grid non-monotonic once the end is appended below.
	tolerance_mm = 1e-6

	while (direction > 0 and current <= end_mm + tolerance_mm) or (direction < 0 and current >= end_mm - tolerance_mm):
		positions.append(round(current, 4))
		current += step_mm * direction

	if positions[-1] != round(end_mm, 4):
		positions.append(round(end_mm, 4))

	return positions




logger = logging.getLogger(__name__)

CELL_MATCH_TOLERANCE_FRACTION = 0.4
CELL_MATCH_TOLERANCE_FALLBACK_MM = 60.0


def _build_scan_cell(
	*,
	cell_idx: int,
	detected_cell: ScanDetectedCell,
	existing_cell: Optional[Cell],
	side_number: int,
) -> Cell:
	status = CellStatus.AVAILABLE
	item = None
	z_offset = 0.0
	is_manual = False
	captured_at_stage_mm = None
	# The scan locates a cell's engagement hole, not the tray it holds: a rebuilt cell comes out
	# unsized so the missing tray dimensions cannot pass for real ones.
	dimensions = Dimensions(width=0.0, height=0.0, length=0.0)

	if existing_cell is not None:
		status = existing_cell.status
		item = existing_cell.item
		# A hand-calibrated cell is the datum every derive/probe run is judged against, and the
		# stage it was captured at is what makes its z comparable. Rebuilding a shelf must hand it
		# back untouched: the operator's point, not the detected one, or the next probe would be
		# judged against a value a model produced.
		is_manual = getattr(existing_cell, "is_manual", False) is True
		if is_manual is True and existing_cell.calibration_point is not None:
			return Cell(
				cell_idx=cell_idx,
				status=status,
				item=item,
				calibration_point=copy.deepcopy(existing_cell.calibration_point),
				dimensions=existing_cell.dimensions,
				is_manual=True,
			)
		if existing_cell.calibration_point is not None:
			z_offset = existing_cell.calibration_point.z_offset
			captured_at_stage_mm = getattr(
				existing_cell.calibration_point, "z_captured_at_stage_mm", None
			)

	return Cell(
		cell_idx=cell_idx,
		status=status,
		item=item,
		calibration_point=CalibrationCoordinate(
			x=detected_cell.tool_x_mm,
			y=detected_cell.tool_y_mm,
			z_offset=z_offset,
			s=side_number,
			z_captured_at_stage_mm=captured_at_stage_mm,
		),
		dimensions=dimensions,
		is_manual=is_manual,
	)


def _scan_cell_match_tolerance_mm(existing_xs: list[float]) -> float:
	if len(existing_xs) < 2:
		return CELL_MATCH_TOLERANCE_FALLBACK_MM

	ordered = sorted(existing_xs)
	min_spacing_mm = min(ordered[index + 1] - ordered[index] for index in range(len(ordered) - 1))
	return min_spacing_mm * CELL_MATCH_TOLERANCE_FRACTION


def _count_offset_matches(detected_xs: list[float], existing_xs: list[float], offset_mm: float, tolerance_mm: float) -> tuple[int, float]:
	is_existing_used = [False] * len(existing_xs)
	match_count = 0
	residual_mm = 0.0

	for detected_x in detected_xs:
		best_index: Optional[int] = None
		best_distance_mm: Optional[float] = None

		for index, existing_x in enumerate(existing_xs):
			if is_existing_used[index] is True:
				continue

			distance_mm = abs(detected_x + offset_mm - existing_x)

			if distance_mm > tolerance_mm:
				continue

			if best_distance_mm is None or distance_mm < best_distance_mm:
				best_distance_mm = distance_mm
				best_index = index

		if best_index is not None:
			is_existing_used[best_index] = True
			match_count += 1
			residual_mm += best_distance_mm or 0.0

	return match_count, residual_mm


def _estimate_frame_offset_mm(detected_xs: list[float], existing_xs: list[float], tolerance_mm: float) -> float:
	if len(detected_xs) == 0 or len(existing_xs) == 0:
		return 0.0

	best_offset_mm = 0.0
	best_match_count = -1
	best_residual_mm: Optional[float] = None

	for detected_x in detected_xs:
		for existing_x in existing_xs:
			offset_mm = existing_x - detected_x
			match_count, residual_mm = _count_offset_matches(detected_xs, existing_xs, offset_mm, tolerance_mm)

			if match_count > best_match_count or (
				match_count == best_match_count
				and best_residual_mm is not None
				and residual_mm < best_residual_mm
			):
				best_match_count = match_count
				best_residual_mm = residual_mm
				best_offset_mm = offset_mm

	return best_offset_mm


def _match_detected_cells_to_existing(
	*,
	detected_cells: list[ScanDetectedCell],
	existing_cells: list[Cell],
	side_number: int,
	shelf_idx: int,
	detected_cell_key_map: Optional[dict[tuple[int, int], tuple[int, int]]] = None,
) -> list[Cell]:
	detected_sorted = sorted(detected_cells, key=lambda cell: float(cell.tool_x_mm))
	calibrated_existing = [
		(cell, cell.calibration_point)
		for cell in existing_cells
		if cell.calibration_point is not None
	]
	uncalibrated_existing = [cell for cell in existing_cells if cell.calibration_point is None]
	existing_sorted = sorted(calibrated_existing, key=lambda pair: float(pair[1].x))

	if len(uncalibrated_existing) == 0 and len(detected_sorted) == len(existing_sorted):
		if detected_cell_key_map is not None:
			for (existing_cell, _existing_point), detected_cell in zip(existing_sorted, detected_sorted):
				detected_cell_key_map[(int(detected_cell.shelf_idx), int(detected_cell.cell_idx))] = (
					shelf_idx,
					int(existing_cell.cell_idx),
				)

		output_cells = [
			_build_scan_cell(
				cell_idx=existing_cell.cell_idx,
				detected_cell=detected_cell,
				existing_cell=existing_cell,
				side_number=side_number,
			)
			for (existing_cell, _existing_point), detected_cell in zip(existing_sorted, detected_sorted)
		]
		output_cells.sort(key=lambda cell: int(cell.cell_idx))
		return output_cells

	existing_xs = [float(point.x) for _, point in existing_sorted]
	tolerance_mm = _scan_cell_match_tolerance_mm(existing_xs)
	frame_offset_mm = _estimate_frame_offset_mm(
		[float(cell.tool_x_mm) for cell in detected_sorted],
		existing_xs,
		tolerance_mm,
	)
	is_detected_used = [False] * len(detected_sorted)
	output_cells = []
	matched_count = 0

	for existing_cell, existing_point in existing_sorted:
		existing_x = float(existing_point.x)
		best_index: Optional[int] = None
		best_distance_mm: Optional[float] = None

		for index, detected_cell in enumerate(detected_sorted):
			if is_detected_used[index] is True:
				continue

			distance_mm = abs(float(detected_cell.tool_x_mm) + frame_offset_mm - existing_x)

			if distance_mm > tolerance_mm:
				continue

			if best_distance_mm is None or distance_mm < best_distance_mm:
				best_distance_mm = distance_mm
				best_index = index

		if best_index is None:
			output_cells.append(existing_cell.model_copy(deep=True))
			continue

		is_detected_used[best_index] = True
		matched_count += 1
		if detected_cell_key_map is not None:
			matched_detected_cell = detected_sorted[best_index]
			detected_cell_key_map[(int(matched_detected_cell.shelf_idx), int(matched_detected_cell.cell_idx))] = (
				shelf_idx,
				int(existing_cell.cell_idx),
			)
		output_cells.append(
			_build_scan_cell(
				cell_idx=existing_cell.cell_idx,
				detected_cell=detected_sorted[best_index],
				existing_cell=existing_cell,
				side_number=side_number,
			)
		)

	for existing_cell in uncalibrated_existing:
		output_cells.append(existing_cell.model_copy(deep=True))

	next_cell_idx = max((int(cell.cell_idx) for cell in output_cells), default=-1) + 1
	extra_count = 0

	for index, detected_cell in enumerate(detected_sorted):
		if is_detected_used[index] is True:
			continue

		if detected_cell_key_map is not None:
			detected_cell_key_map[(int(detected_cell.shelf_idx), int(detected_cell.cell_idx))] = (
				shelf_idx,
				next_cell_idx,
			)

		output_cells.append(
			_build_scan_cell(
				cell_idx=next_cell_idx,
				detected_cell=detected_cell,
				existing_cell=None,
				side_number=side_number,
			)
		)
		next_cell_idx += 1
		extra_count += 1

	preserved_count = len(calibrated_existing) - matched_count

	if preserved_count > 0 or extra_count > 0:
		logger.warning(
			"scan_cell_match_mismatch shelf_idx=%d existing=%d detected=%d matched=%d preserved=%d extra=%d",
			shelf_idx,
			len(existing_cells),
			len(detected_cells),
			matched_count,
			preserved_count,
			extra_count,
		)

	output_cells.sort(key=lambda cell: int(cell.cell_idx))
	return output_cells


def build_shelves_from_scan(
	*,
	side: StorageModuleSide,
	analysis: ScanAnalysisResult,
	existing_shelves: Optional[list[Shelf]] = None,
	detected_cell_key_map: Optional[dict[tuple[int, int], tuple[int, int]]] = None,
) -> list[Shelf]:
	existing_lookup: dict[tuple[int, int], Cell] = {}
	existing_by_shelf: dict[int, Shelf] = {}

	if existing_shelves is not None:
		for shelf in existing_shelves:
			existing_by_shelf[shelf.shelf_idx] = shelf
			for cell in shelf.cells:
				existing_lookup[(shelf.shelf_idx, cell.cell_idx)] = cell

	shelves: list[Shelf] = []
	side_number = 0 if side == StorageModuleSide.FRONT else 1

	for detected_shelf in analysis.shelves:
		existing_shelf = existing_by_shelf.get(detected_shelf.shelf_idx)

		if existing_shelf is not None and len(existing_shelf.cells) > 0:
			cells = _match_detected_cells_to_existing(
				detected_cells=detected_shelf.cells,
				existing_cells=existing_shelf.cells,
				side_number=side_number,
				shelf_idx=detected_shelf.shelf_idx,
				detected_cell_key_map=detected_cell_key_map,
			)
		else:
			if detected_cell_key_map is not None:
				for detected_cell in detected_shelf.cells:
					detected_cell_key_map[(int(detected_cell.shelf_idx), int(detected_cell.cell_idx))] = (
						int(detected_shelf.shelf_idx),
						int(detected_cell.cell_idx),
					)

			cells = [
				_build_scan_cell(
					cell_idx=detected_cell.cell_idx,
					detected_cell=detected_cell,
					existing_cell=existing_lookup.get((detected_cell.shelf_idx, detected_cell.cell_idx)),
					side_number=side_number,
				)
				for detected_cell in detected_shelf.cells
			]

		shelves.append(
			Shelf(
				shelf_idx=detected_shelf.shelf_idx,
				cells=cells,
			)
		)

	return shelves
