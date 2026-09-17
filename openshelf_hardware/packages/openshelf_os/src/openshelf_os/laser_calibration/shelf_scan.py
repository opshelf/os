# ruff: noqa: F401, F403, F405
from __future__ import annotations

from .core import *
from .recording import *
from .motion import *
from openshelf_os.laser_calibration.shelf_scout import (
	build_shelf_scout_x_positions as build_shared_shelf_scout_x_positions,
	filter_shelf_edges as filter_shared_shelf_edges,
	scan_shelf_scout_line as scan_shared_shelf_scout_line,
)

def build_shelf_scout_x_positions(
	*,
	bounds: ScanBounds,
	side: StorageModuleSide,
	scan_config: Any | None = None,
) -> list[float]:
	side_name = 'right' if side == StorageModuleSide.RIGHT else 'left'
	scout_x_mm = None

	if scan_config is not None:
		scout_x_mm = getattr(scan_config, f'{side_name}_shelf_scout_x_mm', None)

	return build_shared_shelf_scout_x_positions(
		x_min_mm=bounds.x_min,
		x_max_mm=bounds.x_max,
		is_right_side=side == StorageModuleSide.RIGHT,
		scout_x_mm=scout_x_mm,
	)

def resolve_lower_boundary_shelf_edge(
	*,
	bounds: ScanBounds,
	side: StorageModuleSide,
	shelf_edges: list[ShelfSurfaceEdge],
	hole_probe_y_offsets_mm: list[float],
) -> ShelfSurfaceEdge | None:
	if len(hole_probe_y_offsets_mm) == 0:
		return None

	edge_x_mm = bounds.x_max

	if side == StorageModuleSide.RIGHT:
		edge_x_mm = bounds.x_min

	seed_y_mm = bounds.y_min

	for shelf_edge in shelf_edges:
		if abs(shelf_edge.y_mm - seed_y_mm) < SHELF_MIN_SPACING_MM:
			return None

	return ShelfSurfaceEdge(
		x_mm=edge_x_mm,
		y_mm=seed_y_mm,
	)


async def capture_shelf_candidates(
	tool: Any,
	*,
	side: StorageModuleSide,
	bounds: ScanBounds,
	settle_ms: int,
	samples_per_point: int,
	scan_config: Any,
	recorder: StagedScanRecorder,
	shelf_scout_only: bool,
	known_shelf_edge_y: Optional[list[float]],
	should_abort: Callable[[], bool] | None = None,
) -> tuple[GridScanResult, float, list[GapCandidateRegion], list[ShelfSurfaceEdge]]:
	coarse_scan = create_sparse_shelf_scan(bounds=bounds)
	scout_x_positions = build_shelf_scout_x_positions(bounds=bounds, side=side, scan_config=scan_config)
	scout_y_positions = list(reversed(build_axis_positions(bounds.y_min, bounds.y_max, SHELF_SCOUT_STEP_MM)))
	hole_sweep_x_positions = build_axis_positions(bounds.x_min, bounds.x_max, SHELF_SWEEP_X_STEP_MM)
	hole_probe_y_offsets_mm = get_shelf_hole_probe_y_offsets_mm(scan_config)
	post_edge_skip_mm = get_shelf_scout_post_edge_skip_mm(scan_config)
	brim_max_height_mm = get_shelf_brim_max_height_mm(scan_config)
	shelf_edges: list[ShelfSurfaceEdge] = []
	horizontal_wall_depth_values: list[list[float | None]] = []
	candidates: list[GapCandidateRegion] = []

	def didPersistScoutProgress(phase: str, progress: dict[str, Any]) -> None:
		save_depth_rows_to_npy(
			output_path=recorder.coarse_depth_path,
			depth_rows=coarse_scan.depth_rows,
		)
		persist_scan_progress(
			recorder=recorder,
			phase=phase,
			progress=progress,
		)

	if known_shelf_edge_y is not None:
		edge_x_mm = bounds.x_max

		if side == StorageModuleSide.RIGHT:
			edge_x_mm = bounds.x_min

		shelf_edges = [
			ShelfSurfaceEdge(
				x_mm=edge_x_mm,
				y_mm=float(y_mm),
			)
			for y_mm in known_shelf_edge_y
		]
	else:
		is_y_brake_disabled = False

		try:
			raise_if_cancelled(should_abort)
			await set_y_brake_for_scan_motion(tool, is_enabled=False)
			is_y_brake_disabled = True

			for scout_idx, scout_x_mm in enumerate(scout_x_positions, start=1):
				raise_if_cancelled(should_abort)
				print_status(f"phase=shelf-scout scout={scout_idx}/{len(scout_x_positions)} x={scout_x_mm:.2f}")

				scout_depth_values, scout_edges = await scan_shelf_scout_line(
					tool,
					x_mm=scout_x_mm,
					y_positions=scout_y_positions,
					settle_ms=settle_ms,
					samples_per_point=samples_per_point,
					progress_label=f"shelf_scout_{scout_idx}",
					post_edge_skip_mm=post_edge_skip_mm,
					brim_max_height_mm=brim_max_height_mm,
					should_abort=should_abort,
				)
				raise_if_cancelled(should_abort)
				merge_line_scan_into_sparse_scan(
					coarse_scan,
					axis="y",
					positions=scout_y_positions,
					fixed_mm=scout_x_mm,
					depth_values=scout_depth_values,
				)
				didPersistScoutProgress(
					"shelf-scout",
					{
						"scout_idx": scout_idx,
						"scout_count": len(scout_x_positions),
						"x_position": scout_x_mm,
					},
				)
				shelf_edges.extend(scout_edges)
		finally:
			if is_y_brake_disabled is True:
				await set_y_brake_for_scan_motion(tool, is_enabled=True)

	filtered_shelf_edges = filter_shared_shelf_edges(
		edges=shelf_edges,
		y_max_mm=bounds.y_max,
	)

	if known_shelf_edge_y is None:
		lower_boundary_edge = resolve_lower_boundary_shelf_edge(
			bounds=bounds,
			side=side,
			shelf_edges=filtered_shelf_edges,
			hole_probe_y_offsets_mm=hole_probe_y_offsets_mm,
		)

		if lower_boundary_edge is not None:
			filtered_shelf_edges.append(lower_boundary_edge)
			filtered_shelf_edges = sorted(filtered_shelf_edges, key=lambda edge: -edge.y_mm)
			print_status(f"lower_boundary_shelf_edge_seeded y={lower_boundary_edge.y_mm:.2f}")

	print_status(
		"shelf_edges="
		f"{len(filtered_shelf_edges)} "
		f"y={[round(edge.y_mm, 2) for edge in filtered_shelf_edges]}"
	)

	if shelf_scout_only is True:
		return coarse_scan, 0.0, [], filtered_shelf_edges

	for shelf_idx, shelf_edge in enumerate(filtered_shelf_edges, start=1):
		raise_if_cancelled(should_abort)
		for offset_idx, offset_mm in enumerate(hole_probe_y_offsets_mm, start=1):
			raise_if_cancelled(should_abort)
			sweep_y_mm = shelf_edge.y_mm + offset_mm

			if sweep_y_mm < bounds.y_min or sweep_y_mm > bounds.y_max:
				continue

			print_status(
				f"phase=shelf-hole-scan shelf={shelf_idx}/{len(filtered_shelf_edges)} "
				f"offset={offset_idx}/{len(hole_probe_y_offsets_mm)} y={sweep_y_mm:.2f}"
			)
			sweep_depth_values = await scan_line_positions(
				tool,
				axis="x",
				positions=hole_sweep_x_positions,
				fixed_mm=sweep_y_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_label=f"shelf_{shelf_idx}_offset_{offset_idx}",
				should_abort=should_abort,
			)
			raise_if_cancelled(should_abort)
			merge_line_scan_into_sparse_scan(
				coarse_scan,
				axis="x",
				positions=hole_sweep_x_positions,
				fixed_mm=sweep_y_mm,
				depth_values=sweep_depth_values,
			)
			didPersistScoutProgress(
				"shelf-hole-scan",
				{
					"shelf_idx": shelf_idx,
					"shelf_count": len(filtered_shelf_edges),
					"offset_idx": offset_idx,
					"offset_count": len(hole_probe_y_offsets_mm),
					"y_position": sweep_y_mm,
				},
			)
			wall_depth_mm, gap_segments = find_gap_segments_in_line(
				values=sweep_depth_values,
				positions=hole_sweep_x_positions,
				step_mm=SHELF_SWEEP_X_STEP_MM,
				scan_config=scan_config,
			)

			if wall_depth_mm > 0:
				horizontal_wall_depth_values.append(
					[
						depth_mm
						for depth_mm in sweep_depth_values
						if depth_mm is not None
					]
				)

			for segment in gap_segments:
				local_wall_depth_mm, local_wall_sample_count = estimate_local_wall_depth_for_segment(
					values=sweep_depth_values,
					segment=segment,
					wall_depth_mm=wall_depth_mm,
					tolerance_mm=scan_config.wall_depth_tolerance_mm,
				)

				candidates.append(
					GapCandidateRegion(
						x_min=segment.start_mm + (SHELF_SWEEP_X_STEP_MM / 2.0),
						x_max=segment.end_mm - (SHELF_SWEEP_X_STEP_MM / 2.0),
						y_min=sweep_y_mm,
						y_max=sweep_y_mm,
						wall_depth_mm=local_wall_depth_mm,
						wall_sample_count=local_wall_sample_count,
					)
				)

	if len(horizontal_wall_depth_values) == 0:
		return coarse_scan, 0.0, [], filtered_shelf_edges

	wall_depth_mm = estimate_wall_depth_mm(horizontal_wall_depth_values)
	return coarse_scan, wall_depth_mm, dedupe_candidate_regions(candidates, scan_config=scan_config), filtered_shelf_edges


def has_wall_nearby(
	row_values: list[float | None],
	*,
	wall_depth_mm: float,
	tolerance_mm: float,
	start_idx: int,
	direction: int,
) -> bool:
	current_idx = start_idx

	for _ in range(COARSE_WALL_SEARCH_STEPS):
		if current_idx < 0 or current_idx >= len(row_values):
			return False

		if is_wall_like_depth(
			row_values[current_idx],
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=tolerance_mm,
		) is True:
			return True

		current_idx += direction

	return False


def build_detected_shelves_from_edges(
	*,
	edges: list[ShelfSurfaceEdge],
	bounds: ScanBounds,
) -> list[ScanDetectedShelf]:
	shelves: list[ScanDetectedShelf] = []

	for shelf_idx, edge in enumerate(sorted(edges, key=lambda current: -current.y_mm)):
		shelves.append(
			ScanDetectedShelf(
				shelf_idx=shelf_idx,
				center_y_mm=edge.y_mm,
				shelf_height_mm=0.0,
				shelf_width_mm=float(bounds.x_max - bounds.x_min),
				cells=[],
			)
		)

	return shelves


def estimate_local_wall_depth_for_segment(
	*,
	values: list[float | None],
	segment: LineGapSegment,
	wall_depth_mm: float,
	tolerance_mm: float,
) -> tuple[float | None, int]:
	wall_values: list[float] = []
	left_limit_idx = max(0, segment.start_idx - COARSE_WALL_SEARCH_STEPS)
	right_limit_idx = min(len(values) - 1, segment.end_idx + COARSE_WALL_SEARCH_STEPS)

	for idx in range(segment.start_idx - 1, left_limit_idx - 1, -1):
		depth_mm = values[idx]

		if is_wall_like_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=tolerance_mm,
		) is False:
			continue

		wall_values.append(float(depth_mm))

	for idx in range(segment.end_idx + 1, right_limit_idx + 1):
		depth_mm = values[idx]

		if is_wall_like_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=tolerance_mm,
		) is False:
			continue

		wall_values.append(float(depth_mm))

	if len(wall_values) == 0:
		return None, 0

	return float(np.median(np.array(wall_values, dtype=np.float32))), len(wall_values)


def find_gap_segments_in_line(
	*,
	values: list[float | None],
	positions: list[float],
	step_mm: float,
	scan_config: Any,
) -> tuple[float, list[LineGapSegment]]:
	wall_depth_mm = estimate_wall_depth_from_line(values)

	if wall_depth_mm is None:
		return 0.0, []

	segments: list[LineGapSegment] = []
	start_idx: Optional[int] = None

	for idx in range(len(values) + 1):
		is_gap = False

		if idx < len(values):
			is_gap = is_hole_interior_depth(
				values[idx],
				wall_depth_mm=wall_depth_mm,
			)

		if is_gap is True and start_idx is None:
			start_idx = idx
			continue

		if is_gap is True or start_idx is None:
			continue

		end_idx = idx - 1
		left_idx = start_idx - 1
		right_idx = end_idx + 1

		if left_idx < 0 or right_idx >= len(values):
			start_idx = None
			continue

		start_mm = positions[start_idx] - (step_mm / 2.0)
		end_mm = positions[end_idx] + (step_mm / 2.0)
		width_mm = float(end_mm - start_mm)

		if width_mm < SHELF_SWEEP_MIN_CANDIDATE_WIDTH_MM:
			start_idx = None
			continue

		if dimensions_match(
			width_mm,
			expected_mm=scan_config.expected_hole_width_mm,
			tolerance_mm=scan_config.hole_width_tolerance_mm + step_mm,
		) is False:
			start_idx = None
			continue

		if has_wall_nearby(
			values,
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=scan_config.wall_depth_tolerance_mm,
			start_idx=left_idx,
			direction=-1,
		) is False:
			start_idx = None
			continue

		if has_wall_nearby(
			values,
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=scan_config.wall_depth_tolerance_mm,
			start_idx=right_idx,
			direction=1,
		) is False:
			start_idx = None
			continue

		segments.append(
			LineGapSegment(
				start_idx=start_idx,
				end_idx=end_idx,
				start_mm=start_mm,
				end_mm=end_mm,
				width_mm=width_mm,
				center_mm=float((start_mm + end_mm) / 2.0),
			)
		)

		start_idx = None

	return wall_depth_mm, segments


def dedupe_candidate_regions(
	candidates: list[GapCandidateRegion],
	*,
	scan_config: Any,
) -> list[GapCandidateRegion]:
	deduped_candidates: list[GapCandidateRegion] = []
	x_threshold_mm = max(scan_config.expected_hole_width_mm / 2.0, SHELF_SWEEP_X_STEP_MM * 2.0)
	y_threshold_mm = max(scan_config.expected_hole_height_mm / 2.0, 10.0)

	for candidate in sorted(candidates, key=lambda current: (current.y_min, current.x_min)):
		matched_candidate: Optional[GapCandidateRegion] = None

		for current in deduped_candidates:
			current_center_x_mm = (current.x_min + current.x_max) / 2.0
			candidate_center_x_mm = (candidate.x_min + candidate.x_max) / 2.0
			current_center_y_mm = (current.y_min + current.y_max) / 2.0
			candidate_center_y_mm = (candidate.y_min + candidate.y_max) / 2.0

			if abs(current_center_x_mm - candidate_center_x_mm) > x_threshold_mm:
				continue

			if abs(current_center_y_mm - candidate_center_y_mm) > y_threshold_mm:
				continue

			matched_candidate = current
			break

		if matched_candidate is None:
			deduped_candidates.append(candidate)
			continue

		matched_candidate.x_min = min(matched_candidate.x_min, candidate.x_min)
		matched_candidate.x_max = max(matched_candidate.x_max, candidate.x_max)
		matched_candidate.y_min = min(matched_candidate.y_min, candidate.y_min)
		matched_candidate.y_max = max(matched_candidate.y_max, candidate.y_max)
		matched_candidate.row_count += candidate.row_count

		if candidate.wall_depth_mm is None or candidate.wall_sample_count <= 0:
			continue

		if matched_candidate.wall_depth_mm is None or matched_candidate.wall_sample_count <= 0:
			matched_candidate.wall_depth_mm = candidate.wall_depth_mm
			matched_candidate.wall_sample_count = candidate.wall_sample_count
			continue

		total_wall_sample_count = matched_candidate.wall_sample_count + candidate.wall_sample_count
		matched_candidate.wall_depth_mm = (
			(matched_candidate.wall_depth_mm * matched_candidate.wall_sample_count)
			+ (candidate.wall_depth_mm * candidate.wall_sample_count)
		) / total_wall_sample_count
		matched_candidate.wall_sample_count = total_wall_sample_count

	return deduped_candidates


def build_fine_scan_bounds(
	region: GapCandidateRegion,
	*,
	overall_bounds: ScanBounds,
	scan_config: Any,
) -> ScanBounds:
	fine_cell_scan_y_margin_mm = get_fine_cell_scan_y_margin_mm(scan_config)
	y_margin_mm = max(
		fine_cell_scan_y_margin_mm + 10.0,
		(scan_config.expected_hole_height_mm / 2.0) + 12.0,
	)

	return build_fine_scan_bounds_with_y_margin(
		region,
		overall_bounds=overall_bounds,
		y_margin_mm=y_margin_mm,
	)


def build_fine_scan_bounds_with_y_margin(
	region: GapCandidateRegion,
	*,
	overall_bounds: ScanBounds,
	y_margin_mm: float,
) -> ScanBounds:

	return ScanBounds(
		x_min=max(overall_bounds.x_min, region.x_min - FINE_SCAN_MARGIN_X_MM),
		x_max=min(overall_bounds.x_max, region.x_max + FINE_SCAN_MARGIN_X_MM),
		y_min=max(overall_bounds.y_min, region.y_min - y_margin_mm),
		y_max=min(overall_bounds.y_max, region.y_max + y_margin_mm),
	)


def build_fine_scan_retry_bounds(
	region: GapCandidateRegion,
	*,
	current_bounds: ScanBounds,
	overall_bounds: ScanBounds,
	scan_config: Any,
) -> ScanBounds:
	current_y_margin_mm = max(0.0, (current_bounds.y_max - current_bounds.y_min) / 2.0)
	retry_y_margin_mm = max(
		current_y_margin_mm + (scan_config.expected_hole_height_mm / 2.0),
		scan_config.expected_hole_height_mm + 6.0,
		FINE_SCAN_RETRY_MIN_Y_MARGIN_MM,
	)

	return build_fine_scan_bounds_with_y_margin(
		region,
		overall_bounds=overall_bounds,
		y_margin_mm=retry_y_margin_mm,
	)


def scan_bounds_match(
	left: ScanBounds,
	right: ScanBounds,
) -> bool:
	tolerance_mm = 0.0001

	if abs(left.x_min - right.x_min) > tolerance_mm:
		return False

	if abs(left.x_max - right.x_max) > tolerance_mm:
		return False

	if abs(left.y_min - right.y_min) > tolerance_mm:
		return False

	if abs(left.y_max - right.y_max) > tolerance_mm:
		return False

	return True


def resolve_target_shelf(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	shelf_idx: int,
) -> Any:
	try:
		target_shelf = grid.get_shelf(side, shelf_idx)
	except IndexError as exc:
		raise ValueError(f"Shelf {shelf_idx} does not exist on {side.value}") from exc

	return target_shelf


def resolve_target_cell(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	target: SingleCellRescanTarget,
) -> Any:
	target_shelf = resolve_target_shelf(
		grid=grid,
		side=side,
		shelf_idx=target.shelf_idx,
	)

	try:
		target_cell = target_shelf.get_cell(target.cell_idx)
	except IndexError as exc:
		raise ValueError(
			f"Cell {target.cell_idx} does not exist on {side.value} shelf {target.shelf_idx}"
		) from exc

	if target_cell.calibration_point is None:
		raise ValueError(
			f"Cell {target.shelf_idx}:{target.cell_idx} has no calibration point"
		)

	return target_cell


def build_single_cell_rescan_candidate(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	target: SingleCellRescanTarget,
	scan_config: Any,
) -> GapCandidateRegion:
	target_cell = resolve_target_cell(
		grid=grid,
		side=side,
		target=target,
	)

	laser_offset_x_mm, laser_offset_y_mm = require_laser_offsets(scan_config)
	xy_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)
	grab_x_mm = float(target_cell.calibration_point.x)
	grab_y_mm = float(target_cell.calibration_point.y)

	if xy_offset is None:
		if side == StorageModuleSide.RIGHT:
			candidate_center_x_mm = grab_x_mm + laser_offset_x_mm
		else:
			candidate_center_x_mm = grab_x_mm - laser_offset_x_mm
		candidate_center_y_mm = grab_y_mm + laser_offset_y_mm
	else:
		candidate_center_x_mm = grab_x_mm - laser_offset_x_mm - xy_offset[0]
		candidate_center_y_mm = grab_y_mm + laser_offset_y_mm - xy_offset[1]

	half_width_mm = float(scan_config.expected_hole_width_mm) / 2.0
	half_height_mm = float(scan_config.expected_hole_height_mm) / 2.0

	return GapCandidateRegion(
		x_min=candidate_center_x_mm - half_width_mm,
		x_max=candidate_center_x_mm + half_width_mm,
		y_min=candidate_center_y_mm - half_height_mm,
		y_max=candidate_center_y_mm + half_height_mm,
		wall_depth_mm=None,
		wall_sample_count=1,
	)


def build_single_cell_rescan_bounds(
	*,
	candidate: GapCandidateRegion,
	scan_config: Any,
) -> ScanBounds:
	x_margin_mm = max(
		float(scan_config.expected_hole_width_mm) + (FINE_SCAN_MARGIN_X_MM * 2.0),
		40.0,
	)
	y_margin_mm = max(
		float(scan_config.expected_hole_height_mm) + (FINE_SCAN_RETRY_MIN_Y_MARGIN_MM * 2.0),
		80.0,
	)

	return ScanBounds(
		x_min=candidate.x_min - x_margin_mm,
		x_max=candidate.x_max + x_margin_mm,
		y_min=candidate.y_min - y_margin_mm,
		y_max=candidate.y_max + y_margin_mm,
	)


def is_point_in_bounds(
	*,
	bounds: ScanBounds,
	x_mm: float,
	y_mm: float,
) -> bool:
	if x_mm < bounds.x_min or x_mm > bounds.x_max:
		return False

	if y_mm < bounds.y_min or y_mm > bounds.y_max:
		return False

	return True

def create_empty_grid_scan(
	*,
	bounds: ScanBounds,
	step_mm: float,
) -> GridScanResult:
	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)

	return GridScanResult(
		x_positions=x_positions,
		y_positions=y_positions,
		depth_rows=[
			[None for _ in x_positions]
			for _ in y_positions
		],
	)


def create_sparse_shelf_scan(
	*,
	bounds: ScanBounds,
) -> GridScanResult:
	return GridScanResult(
		x_positions=build_axis_positions(bounds.x_min, bounds.x_max, SHELF_SWEEP_X_STEP_MM),
		y_positions=build_axis_positions(bounds.y_min, bounds.y_max, SHELF_SCOUT_STEP_MM),
		depth_rows=[
			[None for _ in build_axis_positions(bounds.x_min, bounds.x_max, SHELF_SWEEP_X_STEP_MM)]
			for _ in build_axis_positions(bounds.y_min, bounds.y_max, SHELF_SCOUT_STEP_MM)
		],
	)


def find_nearest_position_index(
	positions: list[float],
	target_mm: float,
) -> int:
	return min(
		range(len(positions)),
		key=lambda idx: abs(positions[idx] - target_mm),
	)


def set_scan_point(
	base_scan: GridScanResult,
	*,
	x_mm: float,
	y_mm: float,
	depth_mm: float | None,
) -> tuple[int, int]:
	col_idx = find_nearest_position_index(base_scan.x_positions, x_mm)
	row_idx = find_nearest_position_index(base_scan.y_positions, y_mm)
	base_scan.depth_rows[row_idx][col_idx] = depth_mm
	return row_idx, col_idx


def estimate_wall_depth_from_line(
	values: list[float | None],
) -> Optional[float]:
	positive_values = [
		float(value)
		for value in values
		if is_positive_depth(value) is True
	]

	if len(positive_values) == 0:
		return None

	return float(np.median(positive_values))


def build_line_run_segment(
	*,
	positions: list[float],
	start_idx: int,
	end_idx: int,
	step_mm: float,
) -> LineGapSegment:
	start_position_mm = float(positions[start_idx])
	end_position_mm = float(positions[end_idx])
	start_mm = min(start_position_mm, end_position_mm) - (step_mm / 2.0)
	end_mm = max(start_position_mm, end_position_mm) + (step_mm / 2.0)

	return LineGapSegment(
		start_idx=start_idx,
		end_idx=end_idx,
		start_mm=start_mm,
		end_mm=end_mm,
		width_mm=float(end_mm - start_mm),
		center_mm=float((start_mm + end_mm) / 2.0),
	)


async def sample_scan_point(
	tool: Any,
	*,
	x_mm: float,
	y_mm: float,
	settle_ms: int,
	samples_per_point: int,
	should_abort: Callable[[], bool] | None = None,
) -> float | None:
	raise_if_cancelled(should_abort)
	await tool._my.move_to_position_no_wait(y_mm)
	await tool._mx.move_to_position_no_wait(x_mm)
	await tool.gantry_wait(should_abort=should_abort)
	raise_if_cancelled(should_abort)
	return await sample_laser_depth(
		tool,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
	)


async def find_hole_interior_idx_from_seed(
	tool: Any,
	*,
	base_scan: GridScanResult,
	axis: str,
	positions: list[float],
	fixed_mm: float,
	start_idx: int,
	progress_prefix: str,
	settle_ms: int,
	samples_per_point: int,
	wall_depth_mm: float,
	on_update: Optional[Callable[[GridScanResult, str, float], None]] = None,
	should_abort: Callable[[], bool] | None = None,
) -> Optional[int]:
	sampled_count = 0
	sampled_depths: dict[int, float | None] = {}
	total_count = len(positions)

	async def didSample(
		position_idx: int,
	) -> float | None:
		nonlocal sampled_count

		if position_idx in sampled_depths:
			return sampled_depths[position_idx]

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

		sampled_depths[position_idx] = depth_mm
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

	for position_idx in range(start_idx, -1, -1):
		raise_if_cancelled(should_abort)
		depth_mm = await didSample(position_idx)

		if is_hole_interior_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
		) is True:
			return position_idx

	for position_idx in range(start_idx + 1, len(positions)):
		raise_if_cancelled(should_abort)
		depth_mm = await didSample(position_idx)

		if is_hole_interior_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
		) is True:
			return position_idx

	return None


async def scan_line_positions(
	tool: Any,
	*,
	axis: str,
	positions: list[float],
	fixed_mm: float,
	settle_ms: int,
	samples_per_point: int,
	progress_label: str,
	progress_every: int = 25,
	should_abort: Callable[[], bool] | None = None,
) -> list[float | None]:
	if len(positions) == 0:
		return []

	first_position_mm = positions[0]
	raise_if_cancelled(should_abort)

	if axis == "x":
		await tool._my.move_to_position_no_wait(fixed_mm)
		await tool._mx.move_to_position_no_wait(first_position_mm)
	else:
		await tool._mx.move_to_position_no_wait(fixed_mm)
		await tool._my.move_to_position_no_wait(first_position_mm)

	await tool.gantry_wait(should_abort=should_abort)

	depth_values: list[float | None] = []

	for position_idx, position_mm in enumerate(positions):
		raise_if_cancelled(should_abort)
		if position_idx > 0:
			if axis == "x":
				await tool._mx.move_to_position_no_wait(position_mm)
			else:
				await tool._my.move_to_position_no_wait(position_mm)

			await tool.gantry_wait(should_abort=should_abort)
		raise_if_cancelled(should_abort)
		depth_mm = await sample_laser_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		depth_values.append(depth_mm)

		if (position_idx + 1) % progress_every != 0 and position_idx + 1 != len(positions):
			continue

		if axis == "x":
			x_mm = position_mm
			y_mm = fixed_mm
		else:
			x_mm = fixed_mm
			y_mm = position_mm

		print_status(
			f"{progress_label}={position_idx + 1}/{len(positions)} "
			f"x={x_mm:.2f} "
			f"y={y_mm:.2f} "
			f"depth={depth_to_label(depth_mm)}"
		)

	return depth_values


async def scan_shelf_scout_line(
	tool: Any,
	*,
	x_mm: float,
	y_positions: list[float],
	settle_ms: int,
	samples_per_point: int,
	progress_label: str,
	progress_every: int = 25,
	post_edge_skip_mm: float = SHELF_SCOUT_POST_EDGE_SKIP_MM,
	brim_max_height_mm: float = SHELF_BRIM_MAX_HEIGHT_MM,
	should_abort: Callable[[], bool] | None = None,
) -> tuple[list[float | None], list[ShelfSurfaceEdge]]:
	return await scan_shared_shelf_scout_line(
		tool,
		x_mm=x_mm,
		y_positions=y_positions,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		progress_label=progress_label,
		sample_depth=sample_laser_depth,
		print_status=print_status,
		progress_every=progress_every,
		post_edge_skip_mm=post_edge_skip_mm,
		brim_max_height_mm=brim_max_height_mm,
		should_abort=should_abort,
	)


def merge_line_scan_into_sparse_scan(
	base_scan: GridScanResult,
	*,
	axis: str,
	positions: list[float],
	fixed_mm: float,
	depth_values: list[float | None],
) -> None:
	for position_mm, depth_mm in zip(positions, depth_values, strict=True):
		if axis == "x":
			x_mm = position_mm
			y_mm = fixed_mm
		else:
			x_mm = fixed_mm
			y_mm = position_mm

		set_scan_point(
			base_scan,
			x_mm=x_mm,
			y_mm=y_mm,
			depth_mm=depth_mm,
		)
