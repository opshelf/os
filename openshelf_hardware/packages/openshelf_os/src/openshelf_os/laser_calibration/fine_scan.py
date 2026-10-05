# ruff: noqa: F401, F403, F405
from __future__ import annotations

from openshelf_os.laser_calibration.defaults import (
	EXPECTED_HOLE_HEIGHT_MM,
	EXPECTED_HOLE_WIDTH_MM,
	HOLE_HEIGHT_TOLERANCE_MM,
	HOLE_WIDTH_TOLERANCE_MM,
)

from .core import *
from .recording import *
from .motion import *
from .shelf_scan import *
from .flying import (
	prefetch_line_depth_values,
	refine_segment_edges_from_samples,
	resolve_axis_rpm_for_feed,
)


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
	should_infer_far_edge: bool = False,
	scan_config: Any = None,
	on_flying_line: Optional[Callable[[str, Any], None]] = None,
) -> Optional[LineGapSegment]:
	prefetched_depth_values: Optional[list[Optional[float]]] = None
	flying_line: Any = None

	if scan_config is not None:
		prefetched = await prefetch_line_depth_values(
			tool,
			scan_config=scan_config,
			axis=axis,
			positions=positions,
			fixed_mm=fixed_mm,
			progress_label=f"{progress_prefix}_flying",
			should_abort=should_abort,
		)

		if prefetched is not None:
			prefetched_depth_values, flying_line = prefetched

	try:
		return await _walk_line_from_center(
			tool,
			base_scan=base_scan,
			axis=axis,
			positions=positions,
			fixed_mm=fixed_mm,
			center_idx=center_idx,
			step_mm=step_mm,
			progress_prefix=progress_prefix,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			wall_depth_mm=wall_depth_mm,
			expected_span_mm=expected_span_mm,
			on_update=on_update,
			should_abort=should_abort,
			should_infer_far_edge=should_infer_far_edge,
			prefetched_depth_values=prefetched_depth_values,
			flying_line=flying_line,
		)
	finally:
		# Report the line once the walk has probed the bins the pass left empty, so the sample
		# log carries those resamples; a cancelled walk still reports what it measured.
		if flying_line is not None and on_flying_line is not None:
			on_flying_line(f"{progress_prefix}_flying", flying_line)


async def _walk_line_from_center(
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
	on_update: Optional[Callable[[GridScanResult, str, float], None]],
	should_abort: Callable[[], bool] | None,
	should_infer_far_edge: bool,
	prefetched_depth_values: Optional[list[Optional[float]]],
	flying_line: Any,
) -> Optional[LineGapSegment]:
	def didRefineSegment(segment: LineGapSegment) -> LineGapSegment:
		if flying_line is None or prefetched_depth_values is None:
			return segment

		return refine_segment_edges_from_samples(
			segment=segment,
			positions=positions,
			depth_values=prefetched_depth_values,
			samples=flying_line.samples,
		)

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

		if prefetched_depth_values is None:
			depth_mm = await sample_scan_point(
				tool,
				x_mm=x_mm,
				y_mm=y_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				should_abort=should_abort,
			)
		else:
			depth_mm = prefetched_depth_values[position_idx]

			# A sparse flying pass leaves grid positions with no sample, and the decision
			# walk reads an empty bin as wall, which truncates the hole and shifts its
			# centre. Probe the position directly and keep the value for edge refinement.
			if depth_mm is None:
				depth_mm = await sample_scan_point(
					tool,
					x_mm=x_mm,
					y_mm=y_mm,
					settle_ms=settle_ms,
					samples_per_point=samples_per_point,
					should_abort=should_abort,
				)
				prefetched_depth_values[position_idx] = depth_mm
				flying_line.record_resample(position_mm, depth_mm)
				print_status(
					f"{progress_prefix}=resampled idx={position_idx} "
					f"x={x_mm:.2f} y={y_mm:.2f} depth={depth_to_label(depth_mm)}"
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

	if left_wall_found is False:
		return None

	if right_wall_found is False:
		if should_infer_far_edge is False:
			return None

		# The topmost shelf has no material above its holes, so the laser reads open space
		# as hole interior and the gap never closes inside the window. Only infer that edge
		# when the gap really did run out of window and what was measured is already
		# hole-sized, so a genuinely unbounded reading cannot pass as a cell.
		if right_gap_idx != len(positions) - 1:
			return None

		measured_span_mm = float(positions[right_gap_idx] - positions[left_gap_idx])

		if measured_span_mm < (float(expected_span_mm) / 2.0):
			return None

		inferred_start_mm = positions[left_gap_idx] - (step_mm / 2.0)
		inferred_end_mm = inferred_start_mm + float(expected_span_mm)
		print_status(
			f"{progress_prefix}=inferred_far_edge measured_span_mm={measured_span_mm:.2f} "
			f"assumed_span_mm={float(expected_span_mm):.2f}"
		)

		refined_segment = didRefineSegment(
			LineGapSegment(
				start_idx=left_gap_idx,
				end_idx=right_gap_idx,
				start_mm=inferred_start_mm,
				end_mm=inferred_end_mm,
				width_mm=float(inferred_end_mm - inferred_start_mm),
				center_mm=float((inferred_start_mm + inferred_end_mm) / 2.0),
			)
		)

		# The far edge was inferred, not measured, so the refined near edge has to move it:
		# keeping the pre-refinement end would shrink the span by the near-edge correction and
		# shift the centre by only half of it.
		return LineGapSegment(
			start_idx=refined_segment.start_idx,
			end_idx=refined_segment.end_idx,
			start_mm=float(refined_segment.start_mm),
			end_mm=float(refined_segment.start_mm) + float(expected_span_mm),
			width_mm=float(expected_span_mm),
			center_mm=float(refined_segment.start_mm) + (float(expected_span_mm) / 2.0),
		)

	start_mm = positions[left_gap_idx] - (step_mm / 2.0)
	end_mm = positions[right_gap_idx] + (step_mm / 2.0)
	width_mm = float(end_mm - start_mm)

	return didRefineSegment(
		LineGapSegment(
			start_idx=left_gap_idx,
			end_idx=right_gap_idx,
			start_mm=start_mm,
			end_mm=end_mm,
			width_mm=width_mm,
			center_mm=float((start_mm + end_mm) / 2.0),
		)
	)


async def refine_horizontal_segment(
	tool: Any,
	*,
	segment: LineGapSegment,
	fixed_mm: float,
	bounds: ScanBounds,
	settle_ms: int,
	samples_per_point: int,
	wall_depth_mm: float,
	expected_span_mm: float,
	progress_prefix: str,
	should_abort: Callable[[], bool] | None = None,
	scan_config: Any = None,
	on_flying_line: Optional[Callable[[str, Any], None]] = None,
	) -> Optional[LineGapSegment]:
	refined_x_min_mm = max(
		float(bounds.x_min),
		float(segment.start_mm) - FINE_X_REFINEMENT_MARGIN_MM,
	)
	refined_x_max_mm = min(
		float(bounds.x_max),
		float(segment.end_mm) + FINE_X_REFINEMENT_MARGIN_MM,
	)

	if refined_x_max_mm <= refined_x_min_mm:
		return None

	positions = build_axis_positions(
		refined_x_min_mm,
		refined_x_max_mm,
		FINE_X_REFINEMENT_STEP_MM,
	)

	if len(positions) < 3:
		return None

	refinement_scan = GridScanResult(
		x_positions=positions,
		y_positions=[fixed_mm],
		depth_rows=[[None for _ in positions]],
	)
	center_idx = find_nearest_position_index(
		positions,
		float(segment.center_mm),
	)
	refined_segment = await probe_line_from_center(
		tool,
		base_scan=refinement_scan,
		axis="x",
		positions=positions,
		fixed_mm=fixed_mm,
		center_idx=center_idx,
		step_mm=FINE_X_REFINEMENT_STEP_MM,
		progress_prefix=progress_prefix,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		wall_depth_mm=wall_depth_mm,
		expected_span_mm=expected_span_mm,
		should_abort=should_abort,
		scan_config=scan_config,
		on_flying_line=on_flying_line,
	)

	if refined_segment is None:
		return None

	print_status(
		f"x_refinement=applied original_center_mm={float(segment.center_mm):.3f} "
		f"refined_center_mm={float(refined_segment.center_mm):.3f} "
		f"delta_mm={float(refined_segment.center_mm) - float(segment.center_mm):.3f} "
		f"step_mm={FINE_X_REFINEMENT_STEP_MM:.3f}"
	)
	return refined_segment


X_REFINEMENT_EARLY_EXIT_ROWS = 3
X_REFINEMENT_EARLY_EXIT_SPREAD_MM = 0.75


def is_row_refinement_settled(valid_segments: list[LineGapSegment]) -> bool:
	"""True once enough accepted rows agree tightly enough that more rows cannot help.

	The median across rows exists to survive rows corrupted by tray edges or contents. Once
	X_REFINEMENT_EARLY_EXIT_ROWS rows agree within X_REFINEMENT_EARLY_EXIT_SPREAD_MM, the
	median is already pinned -- a further row could only shift it by less than the tolerance.
	"""
	if len(valid_segments) < X_REFINEMENT_EARLY_EXIT_ROWS:
		return False

	centers = [float(segment.center_mm) for segment in valid_segments]

	return (max(centers) - min(centers)) <= X_REFINEMENT_EARLY_EXIT_SPREAD_MM


async def refine_horizontal_segment_across_rows(
	tool: Any,
	*,
	segment: LineGapSegment,
	fixed_mm: float,
	bounds: ScanBounds,
	settle_ms: int,
	samples_per_point: int,
	wall_depth_mm: float,
	expected_span_mm: float,
	width_tolerance_mm: float,
	progress_prefix: str,
	should_abort: Callable[[], bool] | None = None,
	scan_config: Any = None,
	on_flying_line: Optional[Callable[[str, Any], None]] = None,
	) -> Optional[LineGapSegment]:
	row_offsets_mm = [0.0, -2.0, 2.0, -4.0, 4.0]
	valid_segments: list[LineGapSegment] = []

	for row_idx, row_offset_mm in enumerate(row_offsets_mm, start=1):
		# Early exit: this block is ~70% of all fine-scan samples. Once enough rows agree
		# tightly on the centre, the remaining rows cannot move the median meaningfully, so
		# probing them is pure cost. Rows that disagree keep us going through all five.
		if is_row_refinement_settled(valid_segments) is True:
			print_status(
				f"x_refinement=early_exit rows={len(valid_segments)} "
				f"probed={row_idx - 1}/{len(row_offsets_mm)}"
			)
			break

		refined_segment = await refine_horizontal_segment(
			tool,
			segment=segment,
			fixed_mm=float(fixed_mm) + float(row_offset_mm),
			bounds=bounds,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			wall_depth_mm=wall_depth_mm,
			expected_span_mm=expected_span_mm,
			progress_prefix=f"{progress_prefix}_r{row_idx}",
			should_abort=should_abort,
			scan_config=scan_config,
			on_flying_line=on_flying_line,
		)

		if refined_segment is None:
			continue

		if dimensions_match(
			refined_segment.width_mm,
			expected_mm=expected_span_mm,
			tolerance_mm=width_tolerance_mm,
		) is False:
			print_status(
				f"x_refinement_row=rejected row={row_idx} "
				f"y={float(fixed_mm) + float(row_offset_mm):.3f} "
				f"center_mm={float(refined_segment.center_mm):.3f} "
				f"width_mm={float(refined_segment.width_mm):.3f}"
			)
			continue

		valid_segments.append(refined_segment)
		print_status(
			f"x_refinement_row=accepted row={row_idx} "
			f"y={float(fixed_mm) + float(row_offset_mm):.3f} "
			f"center_mm={float(refined_segment.center_mm):.3f} "
			f"width_mm={float(refined_segment.width_mm):.3f}"
		)

	if len(valid_segments) == 0:
		return None

	center_values = np.array([float(current.center_mm) for current in valid_segments], dtype=np.float32)
	width_values = np.array([float(current.width_mm) for current in valid_segments], dtype=np.float32)
	median_center_mm = float(np.median(center_values))
	median_width_mm = float(np.median(width_values))
	median_start_mm = median_center_mm - (median_width_mm / 2.0)
	median_end_mm = median_center_mm + (median_width_mm / 2.0)
	selected_segment = min(
		valid_segments,
		key=lambda current: abs(float(current.center_mm) - median_center_mm),
	)
	print_status(
		f"x_refinement=median rows={len(valid_segments)} "
		f"original_center_mm={float(segment.center_mm):.3f} "
		f"median_center_mm={median_center_mm:.3f} "
		f"delta_mm={median_center_mm - float(segment.center_mm):.3f} "
		f"median_width_mm={median_width_mm:.3f}"
	)
	return LineGapSegment(
		start_idx=selected_segment.start_idx,
		end_idx=selected_segment.end_idx,
		start_mm=median_start_mm,
		end_mm=median_end_mm,
		width_mm=median_width_mm,
		center_mm=median_center_mm,
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
		expected_mm=EXPECTED_HOLE_WIDTH_MM,
		tolerance_mm=HOLE_WIDTH_TOLERANCE_MM + step_mm,
	) is False:
		return None

	if dimensions_match(
		y_segment.width_mm,
		expected_mm=EXPECTED_HOLE_HEIGHT_MM,
		tolerance_mm=HOLE_HEIGHT_TOLERANCE_MM + step_mm,
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


def get_fine_settle_ms(config_settle_ms: int) -> int:
	return max(0, config_settle_ms)


def get_fine_samples_per_point(config_samples_per_point: int) -> int:
	return max(1, config_samples_per_point)


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
		EXPECTED_HOLE_HEIGHT_MM * 0.75,
		20.0,
	)


Y_LINE_SNAP_RESIDUAL_MM = 1.5
Y_LINE_SNAP_MIN_CELLS = 5


def _fit_y_line_theil_sen(points: list[tuple[float, float]]) -> tuple[float, float]:
	slopes: list[float] = []

	for first_idx in range(len(points)):
		for second_idx in range(first_idx + 1, len(points)):
			delta_x = points[second_idx][0] - points[first_idx][0]

			if abs(delta_x) < 1e-6:
				continue

			slopes.append((points[second_idx][1] - points[first_idx][1]) / delta_x)

	median_y = float(np.median([y for _, y in points]))

	if len(slopes) == 0:
		return 0.0, median_y

	slope = float(np.median(slopes))
	intercept = float(np.median([y - slope * x for x, y in points]))
	return slope, intercept


def snap_shelf_cells_to_y_line(
	cells: list[ScanDetectedCell],
	*,
	residual_mm: float = Y_LINE_SNAP_RESIDUAL_MM,
	min_cells: int = Y_LINE_SNAP_MIN_CELLS,
) -> None:
	"""Snap per-cell Y outliers to the shelf's robust tilt line.

	The vertical hole probe is a single, noisy Y measurement per cell. Fitting a
	robust (Theil-Sen) line y(x) across the shelf preserves a real cross-shelf tilt
	while pulling detection spikes that deviate by more than residual_mm back onto it.
	"""
	if len(cells) < min_cells:
		return

	points = [(float(cell.center_x_mm), float(cell.center_y_mm)) for cell in cells]
	slope, intercept = _fit_y_line_theil_sen(points)

	for cell in cells:
		fitted_y_mm = intercept + slope * float(cell.center_x_mm)
		residual_offset_mm = float(cell.center_y_mm) - fitted_y_mm

		if abs(residual_offset_mm) <= residual_mm:
			continue

		print_status(
			f"y_line_snap=applied center_x_mm={float(cell.center_x_mm):.1f} "
			f"detected_y_mm={float(cell.center_y_mm):.2f} fitted_y_mm={fitted_y_mm:.2f} "
			f"residual_mm={residual_offset_mm:.2f}"
		)
		cell.tool_y_mm = float(cell.tool_y_mm) - residual_offset_mm
		cell.center_y_mm = fitted_y_mm


def build_detected_shelves(
	detected_cells: list[ScanDetectedCell],
	*,
	side: StorageModuleSide,
	shelf_threshold_mm: float,
) -> list[ScanDetectedShelf]:
	sorted_cells = sorted(detected_cells, key=lambda cell: (cell.center_y_mm, cell.center_x_mm))

	shelves: list[ScanDetectedShelf] = []
	current_shelf: Optional[ScanDetectedShelf] = None
	current_cells: list[ScanDetectedCell] = []

	for cell in sorted_cells:
		if current_shelf is None:
			current_shelf = ScanDetectedShelf(
				shelf_idx=0,
				center_y_mm=cell.center_y_mm,
				shelf_width_mm=cell.width_mm,
			)
			current_cells = [cell]
			continue

		if abs(cell.center_y_mm - current_shelf.center_y_mm) <= shelf_threshold_mm:
			current_cells.append(cell)
			continue

		current_cells.sort(key=lambda current: current.center_x_mm)
		snap_shelf_cells_to_y_line(current_cells)

		for cell_idx, current_cell in enumerate(current_cells):
			current_cell.shelf_idx = current_shelf.shelf_idx
			current_cell.cell_idx = cell_idx

		current_shelf.cells = list(current_cells)
		current_shelf.center_y_mm = float(np.mean([current.center_y_mm for current in current_cells]))
		current_shelf.shelf_width_mm = sum(current.width_mm for current in current_cells)
		shelves.append(current_shelf)

		current_shelf = ScanDetectedShelf(
			shelf_idx=len(shelves),
			center_y_mm=cell.center_y_mm,
			shelf_width_mm=cell.width_mm,
		)
		current_cells = [cell]

	if current_shelf is None:
		return shelves

	current_cells.sort(key=lambda current: current.center_x_mm)
	snap_shelf_cells_to_y_line(current_cells)

	for cell_idx, current_cell in enumerate(current_cells):
		current_cell.shelf_idx = current_shelf.shelf_idx
		current_cell.cell_idx = cell_idx

	current_shelf.cells = list(current_cells)
	current_shelf.center_y_mm = float(np.mean([current.center_y_mm for current in current_cells]))
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
	on_flying_line: Optional[Callable[[str, Any], None]] = None,
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
		EXPECTED_HOLE_HEIGHT_MM / 4.0,
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
			expected_span_mm=EXPECTED_HOLE_WIDTH_MM,
			on_update=on_update,
			should_abort=should_abort,
			scan_config=scan_config,
			on_flying_line=on_flying_line,
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
		scan_config=scan_config,
		on_flying_line=on_flying_line,
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
		expected_span_mm=EXPECTED_HOLE_HEIGHT_MM,
		on_update=on_update,
		should_abort=should_abort,
		should_infer_far_edge=True,
		scan_config=scan_config,
		on_flying_line=on_flying_line,
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
		expected_span_mm=EXPECTED_HOLE_WIDTH_MM,
		on_update=on_update,
		should_abort=should_abort,
		scan_config=scan_config,
		on_flying_line=on_flying_line,
	)

	if refined_horizontal_segment is not None:
		horizontal_segment = refined_horizontal_segment

	precision_horizontal_segment = await refine_horizontal_segment_across_rows(
		tool,
		segment=horizontal_segment,
		fixed_mm=refined_horizontal_y_mm,
		bounds=bounds,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		wall_depth_mm=wall_depth_mm,
		expected_span_mm=EXPECTED_HOLE_WIDTH_MM,
		width_tolerance_mm=HOLE_WIDTH_TOLERANCE_MM + FINE_X_REFINEMENT_STEP_MM,
		progress_prefix=f"{progress_prefix}_h_precision",
		should_abort=should_abort,
		scan_config=scan_config,
		on_flying_line=on_flying_line,
	)

	if precision_horizontal_segment is not None:
		horizontal_segment = precision_horizontal_segment

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
	x_threshold_mm = max(EXPECTED_HOLE_WIDTH_MM / 2.0, 8.0)
	y_threshold_mm = max(EXPECTED_HOLE_HEIGHT_MM / 2.0, 8.0)

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

	deduped_cells.sort(key=lambda cell: (cell.center_y_mm, cell.center_x_mm))
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
