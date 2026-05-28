from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

import numpy as np
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
	shelf_height_mm: float
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


class ScanCaptureResult(BaseModel):
	run_id: str
	side: StorageModuleSide
	bounds: ScanBounds
	step_mm: float
	settle_ms: int
	samples_per_point: int
	laser_offset_x_mm: float
	laser_offset_y_mm: float
	wall_depth_tolerance_mm: float
	expected_hole_width_mm: float
	expected_hole_height_mm: float
	hole_width_tolerance_mm: float
	hole_height_tolerance_mm: float
	min_vertical_wall_coverage_mm: float
	min_horizontal_wall_coverage_mm: float
	x_positions: list[float]
	y_positions: list[float]
	depth_rows: list[list[Optional[float]]]


def build_axis_positions(start_mm: float, end_mm: float, step_mm: float) -> list[float]:
	if step_mm <= 0:
		raise ValueError("step_mm must be positive")

	positions: list[float] = []
	direction = 1.0 if end_mm >= start_mm else -1.0
	current = start_mm

	while (direction > 0 and current <= end_mm + (step_mm / 2.0)) or (direction < 0 and current >= end_mm - (step_mm / 2.0)):
		positions.append(round(current, 4))
		current += step_mm * direction

	if positions[-1] != round(end_mm, 4):
		positions.append(round(end_mm, 4))

	return positions


def create_scan_capture(
	*,
	side: StorageModuleSide,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	laser_offset_x_mm: float,
	laser_offset_y_mm: float,
	wall_depth_tolerance_mm: float = 2.0,
	expected_hole_width_mm: float = 26.0,
	expected_hole_height_mm: float = 30.0,
	hole_width_tolerance_mm: float = 10.0,
	hole_height_tolerance_mm: float = 10.0,
	min_vertical_wall_coverage_mm: float = 10.0,
	min_horizontal_wall_coverage_mm: float = 30.0,
	depth_rows: list[list[Optional[float]]],
) -> ScanCaptureResult:
	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)

	if len(depth_rows) != len(y_positions):
		raise ValueError("depth_rows length does not match generated y positions")

	for row in depth_rows:
		if len(row) != len(x_positions):
			raise ValueError("depth row length does not match generated x positions")

	return ScanCaptureResult(
		run_id=uuid4().hex,
		side=side,
		bounds=bounds,
			step_mm=step_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			laser_offset_x_mm=laser_offset_x_mm,
			laser_offset_y_mm=laser_offset_y_mm,
			wall_depth_tolerance_mm=wall_depth_tolerance_mm,
			expected_hole_width_mm=expected_hole_width_mm,
			expected_hole_height_mm=expected_hole_height_mm,
			hole_width_tolerance_mm=hole_width_tolerance_mm,
			hole_height_tolerance_mm=hole_height_tolerance_mm,
			min_vertical_wall_coverage_mm=min_vertical_wall_coverage_mm,
			min_horizontal_wall_coverage_mm=min_horizontal_wall_coverage_mm,
			x_positions=x_positions,
			y_positions=y_positions,
			depth_rows=depth_rows,
		)


def persist_scan_capture(base_dir: str | Path, capture: ScanCaptureResult) -> tuple[Path, Path]:
	base_path = Path(base_dir)
	run_dir = base_path / capture.run_id
	run_dir.mkdir(parents=True, exist_ok=True)

	depth_array = np.array(
		[
			[np.nan if depth_mm is None else float(depth_mm) for depth_mm in row]
			for row in capture.depth_rows
		],
		dtype=np.float32,
	)

	raw_scan_path = run_dir / "depth.npy"
	metadata_path = run_dir / "metadata.json"

	np.save(raw_scan_path, depth_array)

	metadata_path.write_text(
		json.dumps(
			{
				"run_id": capture.run_id,
				"created_at": datetime.now(timezone.utc).isoformat(),
				"side": capture.side.value,
				"bounds": capture.bounds.model_dump(),
					"step_mm": capture.step_mm,
					"settle_ms": capture.settle_ms,
					"samples_per_point": capture.samples_per_point,
					"laser_offset_x_mm": capture.laser_offset_x_mm,
					"laser_offset_y_mm": capture.laser_offset_y_mm,
					"wall_depth_tolerance_mm": capture.wall_depth_tolerance_mm,
					"expected_hole_width_mm": capture.expected_hole_width_mm,
					"expected_hole_height_mm": capture.expected_hole_height_mm,
					"hole_width_tolerance_mm": capture.hole_width_tolerance_mm,
					"hole_height_tolerance_mm": capture.hole_height_tolerance_mm,
					"min_vertical_wall_coverage_mm": capture.min_vertical_wall_coverage_mm,
					"min_horizontal_wall_coverage_mm": capture.min_horizontal_wall_coverage_mm,
					"x_positions": capture.x_positions,
					"y_positions": capture.y_positions,
				},
				indent=2,
		),
		encoding="utf-8",
	)

	return raw_scan_path, metadata_path


def _find_components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
	height, width = mask.shape
	visited = np.zeros((height, width), dtype=bool)
	components: list[list[tuple[int, int]]] = []

	for row_idx in range(height):
		for col_idx in range(width):
			if bool(mask[row_idx, col_idx]) is False:
				continue

			if bool(visited[row_idx, col_idx]) is True:
				continue

			component: list[tuple[int, int]] = []
			queue: deque[tuple[int, int]] = deque([(row_idx, col_idx)])
			visited[row_idx, col_idx] = True

			while len(queue) > 0:
				current_row, current_col = queue.popleft()
				component.append((current_row, current_col))

				for next_row, next_col in (
					(current_row - 1, current_col),
					(current_row + 1, current_col),
					(current_row, current_col - 1),
					(current_row, current_col + 1),
				):
					if next_row < 0 or next_col < 0:
						continue
					if next_row >= height or next_col >= width:
						continue
					if bool(mask[next_row, next_col]) is False:
						continue
					if bool(visited[next_row, next_col]) is True:
						continue

					visited[next_row, next_col] = True
					queue.append((next_row, next_col))

			components.append(component)

	return components


def _estimate_wall_depth_mm(valid_values: np.ndarray) -> float:
	if valid_values.size == 0:
		raise ValueError("No valid laser readings captured")

	bin_size_mm = 0.5
	min_depth_mm = float(np.floor(np.min(valid_values) / bin_size_mm) * bin_size_mm)
	max_depth_mm = float(np.ceil(np.max(valid_values) / bin_size_mm) * bin_size_mm) + bin_size_mm
	bins = np.arange(min_depth_mm, max_depth_mm + bin_size_mm, bin_size_mm, dtype=np.float32)

	if bins.size < 2:
		return float(np.median(valid_values))

	histogram, edges = np.histogram(valid_values, bins=bins)
	dominant_idx = int(np.argmax(histogram))
	lower_bound_mm = float(edges[dominant_idx])
	upper_bound_mm = float(edges[dominant_idx + 1])
	dominant_values = valid_values[
		(valid_values >= lower_bound_mm) &
		(valid_values < upper_bound_mm)
	]

	if dominant_values.size == 0:
		return float(np.median(valid_values))

	return float(np.median(dominant_values))


def _component_dimensions_mm(
	component: list[tuple[int, int]],
	*,
	x_positions: list[float],
	y_positions: list[float],
	step_mm: float,
) -> tuple[int, int, int, int, float, float]:
	row_indices = [row_idx for row_idx, _ in component]
	col_indices = [col_idx for _, col_idx in component]
	min_col = min(col_indices)
	max_col = max(col_indices)
	min_row = min(row_indices)
	max_row = max(row_indices)
	width_mm = float(abs(x_positions[max_col] - x_positions[min_col]) + step_mm)
	height_mm = float(abs(y_positions[max_row] - y_positions[min_row]) + step_mm)

	return min_row, max_row, min_col, max_col, width_mm, height_mm


def _is_dimension_match(
	actual_mm: float,
	*,
	expected_mm: float,
	tolerance_mm: float,
) -> bool:
	min_mm = expected_mm - tolerance_mm
	max_mm = expected_mm + tolerance_mm

	return min_mm <= actual_mm <= max_mm


def _count_contiguous_wall_in_row(
	wall_mask: np.ndarray,
	*,
	row_idx: int,
	center_col: int,
) -> int:
	width = wall_mask.shape[1]
	count = 0
	col_idx = center_col

	while col_idx >= 0:
		if bool(wall_mask[row_idx, col_idx]) is False:
			break

		count += 1
		col_idx -= 1

	col_idx = center_col + 1

	while col_idx < width:
		if bool(wall_mask[row_idx, col_idx]) is False:
			break

		count += 1
		col_idx += 1

	return count


def analyze_scan_capture(
	capture: ScanCaptureResult,
	*,
	base_dir: str | Path,
) -> ScanAnalysisResult:
	raw_scan_path, metadata_path = persist_scan_capture(base_dir, capture)

	depth_array = np.array(
		[
			[np.nan if depth_mm is None else float(depth_mm) for depth_mm in row]
			for row in capture.depth_rows
		],
		dtype=np.float32,
	)

	valid_mask = np.isfinite(depth_array)
	valid_values = depth_array[valid_mask]

	if valid_values.size == 0:
		raise ValueError("No valid laser readings captured")

	wall_depth_mm = _estimate_wall_depth_mm(valid_values)
	wall_mask = valid_mask & (
		np.abs(depth_array - wall_depth_mm) <= capture.wall_depth_tolerance_mm
	)
	void_mask = np.logical_not(wall_mask)
	components = _find_components(void_mask)

	detected_cells: list[ScanDetectedCell] = []
	min_vertical_rows = max(1, int(np.ceil(capture.min_vertical_wall_coverage_mm / capture.step_mm)))
	min_horizontal_cols = max(1, int(np.ceil(capture.min_horizontal_wall_coverage_mm / capture.step_mm)))
	grid_height, grid_width = depth_array.shape

	for component in components:
		min_row, max_row, min_col, max_col, width_mm, height_mm = _component_dimensions_mm(
			component,
			x_positions=capture.x_positions,
			y_positions=capture.y_positions,
			step_mm=capture.step_mm,
		)

		if min_row == 0 or min_col == 0:
			continue
		if max_row == grid_height - 1 or max_col == grid_width - 1:
			continue
		if _is_dimension_match(
			width_mm,
			expected_mm=capture.expected_hole_width_mm,
			tolerance_mm=capture.hole_width_tolerance_mm,
		) is False:
			continue
		if _is_dimension_match(
			height_mm,
			expected_mm=capture.expected_hole_height_mm,
			tolerance_mm=capture.hole_height_tolerance_mm,
		) is False:
			continue

		left_wall_hits = 0
		right_wall_hits = 0
		for row_idx in range(min_row, max_row + 1):
			if bool(wall_mask[row_idx, min_col - 1]) is True:
				left_wall_hits += 1
			if bool(wall_mask[row_idx, max_col + 1]) is True:
				right_wall_hits += 1

		if left_wall_hits < min_vertical_rows:
			continue
		if right_wall_hits < min_vertical_rows:
			continue

		center_col = int(round((min_col + max_col) / 2.0))
		top_wall_hits = _count_contiguous_wall_in_row(
			wall_mask,
			row_idx=min_row - 1,
			center_col=center_col,
		)
		bottom_wall_hits = _count_contiguous_wall_in_row(
			wall_mask,
			row_idx=max_row + 1,
			center_col=center_col,
		)

		if top_wall_hits < min_horizontal_cols:
			continue
		if bottom_wall_hits < min_horizontal_cols:
			continue

		min_x_mm = min(capture.x_positions[min_col], capture.x_positions[max_col])
		max_x_mm = max(capture.x_positions[min_col], capture.x_positions[max_col])
		min_y_mm = min(capture.y_positions[min_row], capture.y_positions[max_row])
		max_y_mm = max(capture.y_positions[min_row], capture.y_positions[max_row])
		left_edge_mm = min_x_mm - (capture.step_mm / 2.0)
		right_edge_mm = max_x_mm + (capture.step_mm / 2.0)
		top_edge_mm = min_y_mm - (capture.step_mm / 2.0)
		bottom_edge_mm = max_y_mm + (capture.step_mm / 2.0)
		center_x_mm = float((left_edge_mm + right_edge_mm) / 2.0)
		center_y_mm = float((top_edge_mm + bottom_edge_mm) / 2.0)

		detected_cells.append(
				ScanDetectedCell(
					shelf_idx=0,
					cell_idx=0,
					center_x_mm=center_x_mm,
					center_y_mm=center_y_mm,
					tool_x_mm=center_x_mm + capture.laser_offset_x_mm,
					tool_y_mm=center_y_mm - capture.laser_offset_y_mm,
				width_mm=width_mm,
				height_mm=height_mm,
				point_count=len(component),
				bbox_x_min=min_x_mm,
				bbox_x_max=max_x_mm,
				bbox_y_min=min_y_mm,
				bbox_y_max=max_y_mm,
			)
		)

	detected_cells.sort(key=lambda cell: (cell.center_y_mm, -cell.center_x_mm))

	shelves: list[ScanDetectedShelf] = []
	current_shelf: Optional[ScanDetectedShelf] = None
	current_shelf_cells: list[ScanDetectedCell] = []
	current_shelf_threshold = capture.step_mm * 2.0

	for cell in detected_cells:
		if current_shelf is None:
			current_shelf = ScanDetectedShelf(
				shelf_idx=0,
				center_y_mm=cell.center_y_mm,
				shelf_height_mm=cell.height_mm,
				shelf_width_mm=cell.width_mm,
			)
			current_shelf_cells = [cell]
			continue

		if abs(cell.center_y_mm - current_shelf.center_y_mm) <= current_shelf_threshold:
			current_shelf_cells.append(cell)
			continue

		current_shelf_cells.sort(key=lambda current_cell: -current_cell.center_x_mm)
		for cell_idx, shelf_cell in enumerate(current_shelf_cells):
			shelf_cell.shelf_idx = current_shelf.shelf_idx
			shelf_cell.cell_idx = cell_idx

		current_shelf.cells = list(current_shelf_cells)
		current_shelf.center_y_mm = float(np.mean([shelf_cell.center_y_mm for shelf_cell in current_shelf_cells]))
		current_shelf.shelf_height_mm = max(shelf_cell.height_mm for shelf_cell in current_shelf_cells)
		current_shelf.shelf_width_mm = sum(shelf_cell.width_mm for shelf_cell in current_shelf_cells)
		shelves.append(current_shelf)

		current_shelf = ScanDetectedShelf(
			shelf_idx=len(shelves),
			center_y_mm=cell.center_y_mm,
			shelf_height_mm=cell.height_mm,
			shelf_width_mm=cell.width_mm,
		)
		current_shelf_cells = [cell]

	if current_shelf is not None:
		current_shelf_cells.sort(key=lambda current_cell: -current_cell.center_x_mm)
		for cell_idx, shelf_cell in enumerate(current_shelf_cells):
			shelf_cell.shelf_idx = current_shelf.shelf_idx
			shelf_cell.cell_idx = cell_idx

		current_shelf.cells = list(current_shelf_cells)
		current_shelf.center_y_mm = float(np.mean([shelf_cell.center_y_mm for shelf_cell in current_shelf_cells]))
		current_shelf.shelf_height_mm = max(shelf_cell.height_mm for shelf_cell in current_shelf_cells)
		current_shelf.shelf_width_mm = sum(shelf_cell.width_mm for shelf_cell in current_shelf_cells)
		shelves.append(current_shelf)

	return ScanAnalysisResult(
		run_id=capture.run_id,
		side=capture.side,
		bounds=capture.bounds,
		step_mm=capture.step_mm,
		samples_per_point=capture.samples_per_point,
		settle_ms=capture.settle_ms,
		wall_depth_mm=wall_depth_mm,
		wall_depth_tolerance_mm=capture.wall_depth_tolerance_mm,
		expected_hole_width_mm=capture.expected_hole_width_mm,
		expected_hole_height_mm=capture.expected_hole_height_mm,
		min_vertical_wall_coverage_mm=capture.min_vertical_wall_coverage_mm,
		min_horizontal_wall_coverage_mm=capture.min_horizontal_wall_coverage_mm,
		total_points=int(depth_array.size),
		valid_points=int(valid_values.size),
		raw_scan_path=str(raw_scan_path),
		metadata_path=str(metadata_path),
		shelves=shelves,
		created_at=datetime.now(timezone.utc),
	)


def build_shelves_from_scan(
	*,
	side: StorageModuleSide,
	analysis: ScanAnalysisResult,
	existing_shelves: Optional[list[Shelf]] = None,
	default_length_mm: float = 400.0,
) -> list[Shelf]:
	existing_lookup: dict[tuple[int, int], Cell] = {}

	if existing_shelves is not None:
		for shelf in existing_shelves:
			for cell in shelf.cells:
				existing_lookup[(shelf.shelf_idx, cell.cell_idx)] = cell

	shelves: list[Shelf] = []
	side_number = 0 if side == StorageModuleSide.LEFT else 1

	for detected_shelf in analysis.shelves:
		cells: list[Cell] = []

		for detected_cell in detected_shelf.cells:
			existing_cell = existing_lookup.get((detected_cell.shelf_idx, detected_cell.cell_idx))
			status = CellStatus.AVAILABLE
			item = None
			z_offset = 0.0

			if existing_cell is not None:
				status = existing_cell.status
				item = existing_cell.item
				if existing_cell.calibration_point is not None:
					z_offset = existing_cell.calibration_point.z_offset

			cells.append(
				Cell(
					cell_idx=detected_cell.cell_idx,
					status=status,
					item=item,
					calibration_point=CalibrationCoordinate(
						x=detected_cell.tool_x_mm,
						y=detected_cell.tool_y_mm,
						z_offset=z_offset,
						s=side_number,
					),
					dimensions=Dimensions(
						width=detected_cell.width_mm,
						height=detected_cell.height_mm,
						length=default_length_mm,
					),
				)
			)

		shelves.append(
			Shelf(
				shelf_idx=detected_shelf.shelf_idx,
				shelf_height=detected_shelf.shelf_height_mm,
				shelf_width=detected_shelf.shelf_width_mm,
				cells=cells,
			)
		)

	return shelves
