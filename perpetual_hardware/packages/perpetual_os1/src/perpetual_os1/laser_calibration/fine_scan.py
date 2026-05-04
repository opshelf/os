# ruff: noqa: F401, F403, F405
from __future__ import annotations

from .core import *
from .recording import *
from .motion import *
from .shelf_scan import *

async def probe_line_from_center(
	tool: Any,
	*,
	base_scan: GridScanResult,
	axis: str,
	positions: list[float],
	fixed_mm: float,
	center_idx: int,
	step_mm: float,
	progress_prefix: str,
	settle_ms: int,
	samples_per_point: int,
	wall_depth_mm: float,
	expected_span_mm: float,
	on_update: Optional[Callable[[GridScanResult, str, float], None]] = None,
	should_abort: Callable[[], bool] | None = None,
) -> Optional[LineGapSegment]:
	left_gap_idx: Optional[int] = None
	right_gap_idx: Optional[int] = None
	left_wall_found = False
	right_wall_found = False
	sampled_count = 0
	total_count = len(positions)

	async def didSample(
		position_idx: int,
	) -> float | None:
		nonlocal sampled_count

		position_mm = positions[position_idx]

		if axis == "x":
			x_mm = position_mm
			y_mm = fixed_mm
		else:
			x_mm = fixed_mm
			y_mm = position_mm

		depth_mm = await sample_scan_point(
			tool,
			x_mm=x_mm,
			y_mm=y_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			should_abort=should_abort,
		)
		set_scan_point(
			base_scan,
			x_mm=x_mm,
			y_mm=y_mm,
			depth_mm=depth_mm,
		)
		sampled_count += 1
		print_status(
			f"{progress_prefix}={sampled_count}/{total_count} "
			f"x={x_mm:.2f} "
			f"y={y_mm:.2f} "
			f"depth={depth_to_label(depth_mm)}"
		)

		if on_update is not None:
			on_update(base_scan, progress_prefix, position_mm)

		return depth_mm

	raise_if_cancelled(should_abort)
	center_depth_mm = await didSample(center_idx)

	if is_hole_interior_depth(
		center_depth_mm,
		wall_depth_mm=wall_depth_mm,
	) is False:
		return None

	left_gap_idx = center_idx
	right_gap_idx = center_idx

	for position_idx in range(center_idx - 1, -1, -1):
		raise_if_cancelled(should_abort)
		depth_mm = await didSample(position_idx)

		if is_hole_interior_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
		) is True:
			left_gap_idx = position_idx
			continue

		if left_gap_idx is not None:
			left_wall_found = True
			break

	right_search_start_idx = center_idx + 1

	if left_wall_found is True:
		expected_right_start_mm = positions[left_gap_idx] + max(
			expected_span_mm - FINE_OPPOSITE_EDGE_MARGIN_MM,
			step_mm,
		)
		estimated_right_idx = find_nearest_position_index(positions, expected_right_start_mm)
		backtrack_steps = max(1, int(np.ceil(FINE_OPPOSITE_EDGE_BACKTRACK_MM / step_mm)))
		search_floor_idx = max(center_idx + 1, estimated_right_idx - backtrack_steps)
		found_estimated_interior_idx: Optional[int] = None

		for position_idx in range(estimated_right_idx, search_floor_idx - 1, -1):
			raise_if_cancelled(should_abort)
			depth_mm = await didSample(position_idx)

			if is_hole_interior_depth(
				depth_mm,
				wall_depth_mm=wall_depth_mm,
			) is False:
				continue

			found_estimated_interior_idx = position_idx
			right_gap_idx = position_idx
			break

		if found_estimated_interior_idx is not None:
			right_search_start_idx = found_estimated_interior_idx + 1

	for position_idx in range(right_search_start_idx, len(positions)):
		raise_if_cancelled(should_abort)
		depth_mm = await didSample(position_idx)

		if is_hole_interior_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
		) is True:
			right_gap_idx = position_idx
			continue

		if right_gap_idx is not None:
			right_wall_found = True
			break

	if left_gap_idx is None or right_gap_idx is None:
		return None

	if left_wall_found is False or right_wall_found is False:
		return None

	start_mm = positions[left_gap_idx] - (step_mm / 2.0)
	end_mm = positions[right_gap_idx] + (step_mm / 2.0)
	width_mm = float(end_mm - start_mm)

	return LineGapSegment(
		start_idx=left_gap_idx,
		end_idx=right_gap_idx,
		start_mm=start_mm,
		end_mm=end_mm,
		width_mm=width_mm,
		center_mm=float((start_mm + end_mm) / 2.0),
	)


def build_detected_cell_from_segments(
	*,
	x_segment: LineGapSegment,
	y_segment: LineGapSegment,
	step_mm: float,
	scan_config: Any,
) -> Optional[ScanDetectedCell]:
	if dimensions_match(
		x_segment.width_mm,
		expected_mm=scan_config.expected_hole_width_mm,
		tolerance_mm=scan_config.hole_width_tolerance_mm + step_mm,
	) is False:
		return None

	if dimensions_match(
		y_segment.width_mm,
		expected_mm=scan_config.expected_hole_height_mm,
		tolerance_mm=scan_config.hole_height_tolerance_mm + step_mm,
	) is False:
		return None

	center_x_mm = float((x_segment.start_mm + x_segment.end_mm) / 2.0)
	center_y_mm = float((y_segment.start_mm + y_segment.end_mm) / 2.0)
	laser_offset_x_mm, laser_offset_y_mm = require_laser_offsets(scan_config)

	return ScanDetectedCell(
		shelf_idx=0,
		cell_idx=0,
		center_x_mm=center_x_mm,
		center_y_mm=center_y_mm,
		tool_x_mm=center_x_mm + laser_offset_x_mm,
		tool_y_mm=center_y_mm - laser_offset_y_mm,
		width_mm=x_segment.width_mm,
		height_mm=y_segment.width_mm,
		point_count=max(
			x_segment.end_idx - x_segment.start_idx + 1,
			y_segment.end_idx - y_segment.start_idx + 1,
		),
		bbox_x_min=x_segment.start_mm + (step_mm / 2.0),
		bbox_x_max=x_segment.end_mm - (step_mm / 2.0),
		bbox_y_min=y_segment.start_mm + (step_mm / 2.0),
		bbox_y_max=y_segment.end_mm - (step_mm / 2.0),
	)


def get_coarse_settle_ms(fine_settle_ms: int) -> int:
	return max(0, fine_settle_ms // 2)


def get_coarse_samples_per_point(fine_samples_per_point: int) -> int:
	return 1


def get_fine_settle_ms(config_settle_ms: int) -> int:
	return max(20, config_settle_ms // 2)


def get_fine_samples_per_point(config_samples_per_point: int) -> int:
	return 1


def dimensions_match(
	actual_mm: float,
	*,
	expected_mm: float,
	tolerance_mm: float,
) -> bool:
	return (expected_mm - tolerance_mm) <= actual_mm <= (expected_mm + tolerance_mm)


def get_shelf_grouping_threshold_mm(
	*,
	step_mm: float,
	scan_config: Any,
) -> float:
	return max(
		step_mm * 2.0,
		scan_config.expected_hole_height_mm * 0.75,
		20.0,
	)


def build_detected_shelves(
	detected_cells: list[ScanDetectedCell],
	*,
	side: StorageModuleSide,
	shelf_threshold_mm: float,
) -> list[ScanDetectedShelf]:
	if side == StorageModuleSide.RIGHT:
		sorted_cells = sorted(detected_cells, key=lambda cell: (cell.center_y_mm, cell.center_x_mm))
	else:
		sorted_cells = sorted(detected_cells, key=lambda cell: (cell.center_y_mm, -cell.center_x_mm))

	shelves: list[ScanDetectedShelf] = []
	current_shelf: Optional[ScanDetectedShelf] = None
	current_cells: list[ScanDetectedCell] = []

	for cell in sorted_cells:
		if current_shelf is None:
			current_shelf = ScanDetectedShelf(
				shelf_idx=0,
				center_y_mm=cell.center_y_mm,
				shelf_height_mm=cell.height_mm,
				shelf_width_mm=cell.width_mm,
			)
			current_cells = [cell]
			continue

		if abs(cell.center_y_mm - current_shelf.center_y_mm) <= shelf_threshold_mm:
			current_cells.append(cell)
			continue

		if side == StorageModuleSide.RIGHT:
			current_cells.sort(key=lambda current: current.center_x_mm)
		else:
			current_cells.sort(key=lambda current: -current.center_x_mm)

		for cell_idx, current_cell in enumerate(current_cells):
			current_cell.shelf_idx = current_shelf.shelf_idx
			current_cell.cell_idx = cell_idx

		current_shelf.cells = list(current_cells)
		current_shelf.center_y_mm = float(np.mean([current.center_y_mm for current in current_cells]))
		current_shelf.shelf_height_mm = max(current.height_mm for current in current_cells)
		current_shelf.shelf_width_mm = sum(current.width_mm for current in current_cells)
		shelves.append(current_shelf)

		current_shelf = ScanDetectedShelf(
			shelf_idx=len(shelves),
			center_y_mm=cell.center_y_mm,
			shelf_height_mm=cell.height_mm,
			shelf_width_mm=cell.width_mm,
		)
		current_cells = [cell]

	if current_shelf is None:
		return shelves

	if side == StorageModuleSide.RIGHT:
		current_cells.sort(key=lambda current: current.center_x_mm)
	else:
		current_cells.sort(key=lambda current: -current.center_x_mm)

	for cell_idx, current_cell in enumerate(current_cells):
		current_cell.shelf_idx = current_shelf.shelf_idx
		current_cell.cell_idx = cell_idx

	current_shelf.cells = list(current_cells)
	current_shelf.center_y_mm = float(np.mean([current.center_y_mm for current in current_cells]))
	current_shelf.shelf_height_mm = max(current.height_mm for current in current_cells)
	current_shelf.shelf_width_mm = sum(current.width_mm for current in current_cells)
	shelves.append(current_shelf)
	return shelves


async def capture_cross_scan(
	tool: Any,
	*,
	candidate: GapCandidateRegion,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	wall_depth_mm: float,
	scan_config: Any,
	progress_prefix: str,
	on_update: Optional[Callable[[GridScanResult, str, float], None]] = None,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[GridScanResult, list[ScanDetectedCell]]:
	validate_scan_plan(
		bounds=bounds,
		step_mm=step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		label="fine cell scan",
	)
	fine_scan = create_empty_grid_scan(
		bounds=bounds,
		step_mm=step_mm,
	)
	x_positions = fine_scan.x_positions
	y_positions = fine_scan.y_positions
	candidate_center_x_mm = float((candidate.x_min + candidate.x_max) / 2.0)
	candidate_center_y_mm = float((candidate.y_min + candidate.y_max) / 2.0)
	probe_offset_mm = min(
		scan_config.expected_hole_height_mm / 4.0,
		FINE_SCAN_MARGIN_Y_MM,
	)
	horizontal_probe_targets = [
		candidate_center_y_mm,
		candidate_center_y_mm - probe_offset_mm,
		candidate_center_y_mm + probe_offset_mm,
	]
	horizontal_probe_indices: list[int] = []

	for target_y_mm in horizontal_probe_targets:
		raise_if_cancelled(should_abort)
		row_idx = find_nearest_position_index(y_positions, target_y_mm)

		if row_idx in horizontal_probe_indices:
			continue

		horizontal_probe_indices.append(row_idx)

	horizontal_segment: Optional[LineGapSegment] = None

	for probe_idx, row_idx in enumerate(horizontal_probe_indices, start=1):
		raise_if_cancelled(should_abort)
		y_mm = y_positions[row_idx]
		center_x_idx = find_nearest_position_index(x_positions, candidate_center_x_mm)
		horizontal_segment = await probe_line_from_center(
			tool,
			base_scan=fine_scan,
			axis="x",
			positions=x_positions,
			fixed_mm=y_mm,
			center_idx=center_x_idx,
			step_mm=step_mm,
			progress_prefix=f"{progress_prefix}_h{probe_idx}",
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			wall_depth_mm=wall_depth_mm,
			expected_span_mm=scan_config.expected_hole_width_mm,
			on_update=on_update,
			should_abort=should_abort,
		)

		if horizontal_segment is None:
			continue
		break

	if horizontal_segment is None:
		return fine_scan, []

	x_center_mm = horizontal_segment.center_mm
	vertical_probe_x_mm = x_positions[find_nearest_position_index(x_positions, x_center_mm)]
	vertical_center_idx = find_nearest_position_index(y_positions, candidate_center_y_mm)
	vertical_seed_idx = await find_hole_interior_idx_from_seed(
		tool,
		base_scan=fine_scan,
		axis="y",
		positions=y_positions,
		fixed_mm=vertical_probe_x_mm,
		start_idx=vertical_center_idx,
		progress_prefix=f"{progress_prefix}_v_seek",
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		wall_depth_mm=wall_depth_mm,
		on_update=on_update,
		should_abort=should_abort,
	)

	if vertical_seed_idx is None:
		return fine_scan, []

	vertical_segment = await probe_line_from_center(
		tool,
		base_scan=fine_scan,
		axis="y",
		positions=y_positions,
		fixed_mm=vertical_probe_x_mm,
		center_idx=vertical_seed_idx,
		step_mm=step_mm,
		progress_prefix=f"{progress_prefix}_v1",
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		wall_depth_mm=wall_depth_mm,
		expected_span_mm=scan_config.expected_hole_height_mm,
		on_update=on_update,
		should_abort=should_abort,
	)

	if vertical_segment is None:
		return fine_scan, []

	refined_center_y_mm = vertical_segment.center_mm
	refined_horizontal_y_mm = y_positions[find_nearest_position_index(y_positions, refined_center_y_mm)]
	refined_horizontal_segment = await probe_line_from_center(
		tool,
		base_scan=fine_scan,
		axis="x",
		positions=x_positions,
		fixed_mm=refined_horizontal_y_mm,
		center_idx=find_nearest_position_index(x_positions, horizontal_segment.center_mm),
		step_mm=step_mm,
		progress_prefix=f"{progress_prefix}_h_final",
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		wall_depth_mm=wall_depth_mm,
		expected_span_mm=scan_config.expected_hole_width_mm,
		on_update=on_update,
		should_abort=should_abort,
	)

	if refined_horizontal_segment is not None:
		horizontal_segment = refined_horizontal_segment

	detected_cell = build_detected_cell_from_segments(
		x_segment=horizontal_segment,
		y_segment=vertical_segment,
		step_mm=step_mm,
		scan_config=scan_config,
	)

	if detected_cell is None:
		return fine_scan, []

	return fine_scan, [detected_cell]


def dedupe_detected_cells(
	detected_cells: list[ScanDetectedCell],
	*,
	scan_config: Any,
) -> list[ScanDetectedCell]:
	deduped_cells: list[ScanDetectedCell] = []
	x_threshold_mm = max(scan_config.expected_hole_width_mm / 2.0, 8.0)
	y_threshold_mm = max(scan_config.expected_hole_height_mm / 2.0, 8.0)

	for cell in sorted(detected_cells, key=lambda current: (current.center_y_mm, current.center_x_mm)):
		existing_cell: Optional[ScanDetectedCell] = None

		for current in deduped_cells:
			if abs(current.center_x_mm - cell.center_x_mm) > x_threshold_mm:
				continue
			if abs(current.center_y_mm - cell.center_y_mm) > y_threshold_mm:
				continue

			existing_cell = current
			break

		if existing_cell is None:
			deduped_cells.append(cell)
			continue

		if cell.point_count > existing_cell.point_count:
			deduped_cells.remove(existing_cell)
			deduped_cells.append(cell)

	deduped_cells.sort(key=lambda cell: (cell.center_y_mm, -cell.center_x_mm))
	return deduped_cells


def persist_staged_scan(
	*,
	scans_dir: str,
	side: StorageModuleSide,
	bounds: ScanBounds,
	fine_step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	coarse_settle_ms: int,
	coarse_samples_per_point: int,
	coarse_scan: GridScanResult,
	candidates: list[GapCandidateRegion],
	shelf_edges: list[ShelfSurfaceEdge],
	fine_scan_results: list[tuple[ScanBounds, GridScanResult, list[ScanDetectedCell]]],
	wall_depth_mm: float,
	scan_started_at: Optional[str],
	recorder: Optional[StagedScanRecorder] = None,
) -> tuple[str, str, str]:
	if recorder is None:
		recorder = create_staged_scan_recorder(
			scans_dir=scans_dir,
			run_parameters={},
		)

	save_depth_rows_to_npy(
		output_path=recorder.coarse_depth_path,
		depth_rows=coarse_scan.depth_rows,
	)

	fine_entries: list[dict[str, Any]] = []

	for fine_idx, (fine_bounds, fine_scan, detected_cells) in enumerate(fine_scan_results):
		fine_path = recorder.run_dir / f"fine_{fine_idx:02d}.npy"
		save_depth_rows_to_npy(
			output_path=fine_path,
			depth_rows=fine_scan.depth_rows,
		)
		fine_entries.append(
			{
				"candidate_index": fine_idx,
				"status": "completed",
				"bounds": fine_bounds.model_dump(),
				"raw_scan_path": str(fine_path),
				"x_positions": fine_scan.x_positions,
				"y_positions": fine_scan.y_positions,
				"detected_cells": [cell.model_dump() for cell in detected_cells],
			}
		)

	persist_staged_metadata(
		recorder=recorder,
		side=side,
		bounds=bounds,
		fine_step_mm=fine_step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		coarse_settle_ms=coarse_settle_ms,
		coarse_samples_per_point=coarse_samples_per_point,
		wall_depth_mm=wall_depth_mm,
		coarse_scan=coarse_scan,
		candidates=candidates,
		fine_entries=fine_entries,
		status="completed",
		scan_started_at=scan_started_at,
		shelf_edges=shelf_edges,
	)

	return recorder.run_id, str(recorder.coarse_depth_path), str(recorder.metadata_path)
