from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Optional
from uuid import uuid4

import numpy as np
from pydantic import BaseModel, Field

from perpetual_os1.storage import (
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


class ScanPointReading(BaseModel):
	x_mm: float
	y_mm: float
	depth_mm: Optional[float] = None


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
	surface_depth_mm: float
	depth_threshold_mm: float
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
	depth_threshold_mm: float
	min_component_points: int
	x_positions: list[float]
	y_positions: list[float]
	depth_rows: list[list[Optional[float]]]


def build_axis_positions(start_mm: float, end_mm: float, step_mm: float) -> list[float]:
	if step_mm <= 0:
		raise ValueError("step_mm must be positive")

	if end_mm < start_mm:
		raise ValueError("end_mm must be greater than or equal to start_mm")

	positions: list[float] = []
	current = start_mm

	while current <= end_mm + (step_mm / 2.0):
		positions.append(round(current, 4))
		current += step_mm

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
	depth_threshold_mm: float,
	min_component_points: int,
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
		depth_threshold_mm=depth_threshold_mm,
		min_component_points=min_component_points,
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
				"depth_threshold_mm": capture.depth_threshold_mm,
				"min_component_points": capture.min_component_points,
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

	surface_depth_mm = float(np.median(valid_values))
	hole_mask = valid_mask & (depth_array >= (surface_depth_mm + capture.depth_threshold_mm))
	components = _find_components(hole_mask)

	detected_cells: list[ScanDetectedCell] = []

	for component in components:
		if len(component) < capture.min_component_points:
			continue

		row_indices = [row_idx for row_idx, _ in component]
		col_indices = [col_idx for _, col_idx in component]
		center_x_mm = float(np.mean([capture.x_positions[col_idx] for col_idx in col_indices]))
		center_y_mm = float(np.mean([capture.y_positions[row_idx] for row_idx in row_indices]))
		min_col = min(col_indices)
		max_col = max(col_indices)
		min_row = min(row_indices)
		max_row = max(row_indices)
		width_mm = float(capture.x_positions[max_col] - capture.x_positions[min_col] + capture.step_mm)
		height_mm = float(capture.y_positions[max_row] - capture.y_positions[min_row] + capture.step_mm)

		detected_cells.append(
			ScanDetectedCell(
				shelf_idx=0,
				cell_idx=0,
				center_x_mm=center_x_mm,
				center_y_mm=center_y_mm,
				tool_x_mm=center_x_mm - capture.laser_offset_x_mm,
				tool_y_mm=center_y_mm - capture.laser_offset_y_mm,
				width_mm=width_mm,
				height_mm=height_mm,
				point_count=len(component),
				bbox_x_min=capture.x_positions[min_col],
				bbox_x_max=capture.x_positions[max_col],
				bbox_y_min=capture.y_positions[min_row],
				bbox_y_max=capture.y_positions[max_row],
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
		surface_depth_mm=surface_depth_mm,
		depth_threshold_mm=capture.depth_threshold_mm,
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
