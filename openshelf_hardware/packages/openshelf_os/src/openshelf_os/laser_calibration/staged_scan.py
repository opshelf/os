# ruff: noqa: F401, F403, F405
from __future__ import annotations

from openshelf_os.laser_calibration.defaults import (
	EXPECTED_HOLE_HEIGHT_MM,
	EXPECTED_HOLE_WIDTH_MM,
	HOLE_HEIGHT_TOLERANCE_MM,
	HOLE_WIDTH_TOLERANCE_MM,
	MIN_HORIZONTAL_WALL_COVERAGE_MM,
	MIN_VERTICAL_WALL_COVERAGE_MM,
	WALL_DEPTH_TOLERANCE_MM,
)
from openshelf_os.storage import CellStatus, Dimensions

from . import core as core_module
from .core import *
from .recording import *
from .motion import *
from .shelf_scan import *
from .fine_scan import *

async def run_staged_scan(
	*,
	robot: Robot,
	side: StorageModuleSide,
	bounds: ScanBounds,
	fine_step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	coarse_settle_ms: int,
	coarse_samples_per_point: int,
	scan_config: Any,
	scans_dir: str,
	run_parameters: dict[str, Any],
	shelf_scout_only: bool,
	known_shelf_edge_y: Optional[list[float]],
	seeded_candidates: Optional[list[GapCandidateRegion]] = None,
	seeded_wall_depth_mm: float | None = None,
	recorder: Optional[StagedScanRecorder] = None,
	should_abort: Callable[[], bool] | None = None,
) -> ScanAnalysisResult:
	tool = robot._tool
	validate_scan_plan(
		bounds=bounds,
		step_mm=fine_step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		should_validate_point_count=False,
	)
	validate_laser_structure_parameters(
		scan_config=scan_config,
		side=side,
	)
	if recorder is None:
		recorder = create_staged_scan_recorder(
			scans_dir=scans_dir,
			run_parameters=run_parameters,
		)

	coarse_scan = GridScanResult(x_positions=[], y_positions=[], depth_rows=[])
	candidates: list[GapCandidateRegion] = []
	shelf_edges: list[ShelfSurfaceEdge] = []
	wall_depth_mm = 0.0
	metadata: dict[str, Any] = {}
	fine_entries: list[dict[str, Any]] = []
	fine_scan_results: list[tuple[ScanBounds, GridScanResult, list[ScanDetectedCell]]] = []
	all_detected_cells: list[ScanDetectedCell] = []
	scan_started_at: Optional[str] = None

	previous_scan_log_path = core_module.CURRENT_SCAN_LOG_PATH
	core_module.CURRENT_SCAN_LOG_PATH = recorder.log_path
	print_status(f"run_dir={recorder.run_dir}")

	if recorder.metadata_path.exists() is True:
		metadata = load_json_file(recorder.metadata_path)
		candidates = load_candidates_from_metadata(metadata)
		shelf_edges = [
			ShelfSurfaceEdge(
				x_mm=float(edge["x_mm"]),
				y_mm=float(edge["y_mm"]),
			)
			for edge in metadata.get("shelf_edges", [])
		]
		wall_depth_mm = float(metadata.get("wall_depth_mm", 0.0))
		fine_entries = list(metadata.get("fine_scans", []))
		scan_started_at = metadata.get("scan_started_at")

	if recorder.coarse_depth_path.exists() is True:
		coarse_scan = load_partial_grid_scan(
			npy_path=recorder.coarse_depth_path,
			bounds=bounds,
			step_mm=COARSE_SCAN_STEP_MM,
		)

	for fine_entry in fine_entries:
		if fine_entry.get("status") != "completed":
			continue

		fine_path = Path(fine_entry["raw_scan_path"])

		if fine_path.exists() is False:
			continue

		fine_bounds = ScanBounds.model_validate(fine_entry["bounds"])
		fine_scan = load_partial_grid_scan(
			npy_path=fine_path,
			bounds=fine_bounds,
			step_mm=fine_step_mm,
		)
		detected_cells = [
			ScanDetectedCell.model_validate(cell)
			for cell in fine_entry.get("detected_cells", [])
		]
		fine_scan_results.append((fine_bounds, fine_scan, detected_cells))
		all_detected_cells.extend(detected_cells)

	if recorder.metadata_path.exists() is False:
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
			status="starting",
			scan_started_at=scan_started_at,
			shelf_edges=shelf_edges,
		)

	try:
		raise_if_cancelled(should_abort)
		if scan_started_at is None:
			scan_started_at = get_current_timestamp()
			persist_scan_started_at(
				recorder=recorder,
				scan_started_at=scan_started_at,
			)

		await enter_scan_mode(tool, side, scan_config)
		raise_if_cancelled(should_abort)
		if len(candidates) == 0:
			if seeded_candidates is not None and len(seeded_candidates) > 0:
				candidates = list(seeded_candidates)

				if seeded_wall_depth_mm is not None:
					wall_depth_mm = float(seeded_wall_depth_mm)


				if wall_depth_mm <= 0.0:
					def didRecordSeededFlyingLine(probe_label: str, flying_line: Any) -> None:
						write_flying_line_samples(
							run_dir=recorder.run_dir,
							label=probe_label,
							line=flying_line,
						)

					await estimate_seeded_candidate_wall_depths(
						tool,
						bounds=bounds,
						candidates=candidates,
						scan_config=scan_config,
						coarse_scan=coarse_scan,
						should_abort=should_abort,
						on_flying_line=didRecordSeededFlyingLine,
					)
				if wall_depth_mm <= 0.0:
					seeded_wall_depth_values = [
						float(candidate.wall_depth_mm)
						for candidate in candidates
						if candidate.wall_depth_mm is not None and candidate.wall_depth_mm > 0.0
					]

					if len(seeded_wall_depth_values) > 0:

						seeded_wall_depth_array = np.array(seeded_wall_depth_values, dtype=np.float32)

						wall_depth_mm = float(np.median(seeded_wall_depth_array))

				if wall_depth_mm <= 0.0:
					raise ValueError("Seeded fine scan requires a valid wall depth")

				print_status(
					f"phase=seeded-fine candidates={len(candidates)} "
					f"wall_depth_mm={wall_depth_mm:.2f}"
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
					status="coarse-detect",
					scan_started_at=scan_started_at,
					shelf_edges=shelf_edges,
				)
			else:
				print_status("phase=coarse-scan")

				coarse_scan, wall_depth_mm, candidates, shelf_edges = await capture_shelf_candidates(
					tool,
					side=side,
					bounds=bounds,
					settle_ms=coarse_settle_ms,
					samples_per_point=coarse_samples_per_point,
					scan_config=scan_config,
					recorder=recorder,
					shelf_scout_only=shelf_scout_only,
					known_shelf_edge_y=known_shelf_edge_y,
					should_abort=should_abort,
				)
				raise_if_cancelled(should_abort)

				print_status("phase=coarse-detect")
				print_status(
					f"coarse_candidates={len(candidates)} "
					f"coarse_rows={len([row for row in coarse_scan.depth_rows if any(value is not None for value in row)])} "
					f"wall_depth_mm={wall_depth_mm:.2f}"
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
					status="coarse-detect",
					scan_started_at=scan_started_at,
					shelf_edges=shelf_edges,
				)
		else:
			print_status(
				f"resume=fine candidates={len(candidates)} "
				f"completed_candidates={len(fine_scan_results)}"
			)

		is_y_brake_disabled = False

		try:
			if len(candidates) > 0:
				raise_if_cancelled(should_abort)
				await set_y_brake_for_scan_motion(tool, is_enabled=False)
				is_y_brake_disabled = True

			for candidate_idx, candidate in enumerate(candidates, start=1):
				raise_if_cancelled(should_abort)
				if candidate_idx <= len(fine_entries):
					existing_fine_entry = fine_entries[candidate_idx - 1]
				else:
					existing_fine_entry = {}

				if existing_fine_entry.get("status") in {"completed", "aborted"}:
					continue

				fine_bounds = build_fine_scan_bounds(
					candidate,
					overall_bounds=bounds,
					scan_config=scan_config,
				)
				selected_fine_bounds = fine_bounds
				candidate_wall_depth_mm = wall_depth_mm

				if candidate.wall_depth_mm is not None:
					candidate_wall_depth_mm = float(candidate.wall_depth_mm)

				print_status(
					f"phase=fine-scan candidate={candidate_idx}/{len(candidates)} "
					f"bounds:x=({fine_bounds.x_min:.2f},{fine_bounds.x_max:.2f}) "
					f"y=({fine_bounds.y_min:.2f},{fine_bounds.y_max:.2f})"
				)

				fine_path = recorder.run_dir / f"fine_{candidate_idx - 1:02d}.npy"

				while len(fine_entries) < candidate_idx:
					fine_entries.append({})

				fine_entries[candidate_idx - 1] = {
					"candidate_index": candidate_idx - 1,
					"status": "running",
					"bounds": fine_bounds.model_dump(),
					"raw_scan_path": str(fine_path),
					"x_positions": existing_fine_entry.get("x_positions", []),
					"y_positions": existing_fine_entry.get("y_positions", []),
					"detected_cells": existing_fine_entry.get("detected_cells", []),
				}

				def didUpdateFineScan(
					partial_scan: GridScanResult,
					probe_label: str,
					probe_position_mm: float,
				) -> None:
					save_depth_rows_to_npy(
						output_path=fine_path,
						depth_rows=partial_scan.depth_rows,
					)
					persist_scan_progress(
						recorder=recorder,
						phase="fine-scan",
						progress={
							"candidate_idx": candidate_idx,
							"candidate_count": len(candidates),
							"probe": probe_label,
							"probe_position_mm": probe_position_mm,
							"fine_path": str(fine_path),
						},
					)
					fine_entries[candidate_idx - 1]["x_positions"] = partial_scan.x_positions
					fine_entries[candidate_idx - 1]["y_positions"] = partial_scan.y_positions

				def didRecordFlyingLine(probe_label: str, flying_line: Any) -> None:
					write_flying_line_samples(
						run_dir=recorder.run_dir,
						label=probe_label,
						line=flying_line,
					)

				# A retry re-flies the same horizontal rows, so the first attempt's lines are kept for it.
				with reuse_flown_fine_lines():
					fine_scan, detected_cells = await capture_cross_scan(
						tool,
						candidate=candidate,
						bounds=selected_fine_bounds,
						step_mm=fine_step_mm,
						settle_ms=settle_ms,
						samples_per_point=samples_per_point,
						wall_depth_mm=candidate_wall_depth_mm,
						scan_config=scan_config,
						progress_prefix=f"fine_{candidate_idx}",
						on_update=didUpdateFineScan,
						should_abort=should_abort,
						on_flying_line=didRecordFlyingLine,
					)
					raise_if_cancelled(should_abort)

					if len(detected_cells) == 0:
						retry_fine_bounds = build_fine_scan_retry_bounds(
							candidate,
							current_bounds=selected_fine_bounds,
							overall_bounds=bounds,
							scan_config=scan_config,
						)

						if scan_bounds_match(retry_fine_bounds, selected_fine_bounds) is False:
							print_status(
								f"phase=fine-scan-retry candidate={candidate_idx}/{len(candidates)} "
								f"bounds:x=({retry_fine_bounds.x_min:.2f},{retry_fine_bounds.x_max:.2f}) "
								f"y=({retry_fine_bounds.y_min:.2f},{retry_fine_bounds.y_max:.2f})"
							)
							fine_scan, detected_cells = await capture_cross_scan(
								tool,
								candidate=candidate,
								bounds=retry_fine_bounds,
								step_mm=fine_step_mm,
								settle_ms=settle_ms,
								samples_per_point=samples_per_point,
								wall_depth_mm=candidate_wall_depth_mm,
								scan_config=scan_config,
								progress_prefix=f"fine_{candidate_idx}_retry",
								on_update=didUpdateFineScan,
								should_abort=should_abort,
								on_flying_line=didRecordFlyingLine,
							)
							raise_if_cancelled(should_abort)
							selected_fine_bounds = retry_fine_bounds

				print_status(
					f"fine_detected_cells={len(detected_cells)} "
					f"candidate={candidate_idx}/{len(candidates)}"
				)
				fine_scan_results.append((selected_fine_bounds, fine_scan, detected_cells))
				all_detected_cells.extend(detected_cells)
				fine_entries[candidate_idx - 1] = {
					"candidate_index": candidate_idx - 1,
					"status": "completed",
					"bounds": selected_fine_bounds.model_dump(),
					"raw_scan_path": str(fine_path),
					"x_positions": fine_scan.x_positions,
					"y_positions": fine_scan.y_positions,
					"detected_cells": [cell.model_dump() for cell in detected_cells],
				}
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
					status="fine-scan",
					scan_started_at=scan_started_at,
					shelf_edges=shelf_edges,
				)
		finally:
			if is_y_brake_disabled is True:
				await set_y_brake_for_scan_motion(tool, is_enabled=True)
	finally:
		core_module.CURRENT_SCAN_LOG_PATH = previous_scan_log_path

		if should_skip_motion_cleanup_on_cancel() is False:
			await tool.retract()

	deduped_cells = dedupe_detected_cells(
		all_detected_cells,
		scan_config=scan_config,
	)

	if shelf_scout_only is True:
		shelves = build_detected_shelves_from_edges(
			edges=shelf_edges,
			bounds=bounds,
		)
	else:
		shelves = build_detected_shelves(
			deduped_cells,
			side=side,
			shelf_threshold_mm=get_shelf_grouping_threshold_mm(
				step_mm=fine_step_mm,
				scan_config=scan_config,
			),
		)
	run_id, raw_scan_path, metadata_path = persist_staged_scan(
		scans_dir=scans_dir,
		side=side,
		bounds=bounds,
		fine_step_mm=fine_step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		coarse_settle_ms=coarse_settle_ms,
		coarse_samples_per_point=coarse_samples_per_point,
		coarse_scan=coarse_scan,
		candidates=candidates,
		shelf_edges=shelf_edges,
		fine_scan_results=fine_scan_results,
		wall_depth_mm=wall_depth_mm,
		scan_started_at=scan_started_at,
		recorder=recorder,
	)
	persist_scan_progress(
		recorder=recorder,
		phase="completed",
		progress={
			"candidate_count": len(candidates),
			"detected_cell_count": len(deduped_cells),
		},
	)

	return ScanAnalysisResult(
		run_id=run_id,
		side=side,
		bounds=bounds,
		step_mm=fine_step_mm,
		samples_per_point=samples_per_point,
		settle_ms=settle_ms,
		wall_depth_mm=wall_depth_mm,
		wall_depth_tolerance_mm=WALL_DEPTH_TOLERANCE_MM,
		expected_hole_width_mm=EXPECTED_HOLE_WIDTH_MM,
		expected_hole_height_mm=EXPECTED_HOLE_HEIGHT_MM,
		min_vertical_wall_coverage_mm=MIN_VERTICAL_WALL_COVERAGE_MM,
		min_horizontal_wall_coverage_mm=MIN_HORIZONTAL_WALL_COVERAGE_MM,
		total_points=count_valid_points(coarse_scan.depth_rows) + sum(
			count_valid_points(fine_scan.depth_rows)
			for _, fine_scan, _ in fine_scan_results
		),
		valid_points=count_valid_points(coarse_scan.depth_rows) + sum(
			count_valid_points(fine_scan.depth_rows)
			for _, fine_scan, _ in fine_scan_results
		),
		raw_scan_path=raw_scan_path,
		metadata_path=metadata_path,
		shelves=shelves,
		created_at=datetime.now(timezone.utc),
	)


def get_candidate_center(candidate: GapCandidateRegion) -> tuple[float, float]:
	return (
		float((candidate.x_min + candidate.x_max) / 2.0),
		float((candidate.y_min + candidate.y_max) / 2.0),
	)


def find_candidate_for_detected_cell(
	*,
	detected_cell: ScanDetectedCell,
	candidates: list[GapCandidateRegion],
) -> GapCandidateRegion | None:
	x_tolerance_mm = max(float(detected_cell.width_mm), 12.0)
	y_tolerance_mm = max(float(detected_cell.height_mm), 12.0)
	matched_candidate: GapCandidateRegion | None = None
	matched_distance_mm: float | None = None

	for candidate in candidates:
		candidate_center_x_mm, candidate_center_y_mm = get_candidate_center(candidate)
		x_delta_mm = abs(candidate_center_x_mm - float(detected_cell.center_x_mm))
		y_delta_mm = abs(candidate_center_y_mm - float(detected_cell.center_y_mm))

		if x_delta_mm > x_tolerance_mm:
			continue

		if y_delta_mm > y_tolerance_mm:
			continue

		distance_mm = float(np.hypot(x_delta_mm, y_delta_mm))

		if matched_distance_mm is not None and distance_mm >= matched_distance_mm:
			continue

		matched_candidate = candidate
		matched_distance_mm = distance_mm

	return matched_candidate


def build_candidate_lookup_by_cell(
	*,
	analysis: Any,
	candidates: list[GapCandidateRegion],
) -> dict[tuple[int, int], GapCandidateRegion]:
	candidate_lookup: dict[tuple[int, int], GapCandidateRegion] = {}

	for shelf in analysis.shelves:
		for cell in shelf.cells:
			matched_candidate = find_candidate_for_detected_cell(
				detected_cell=cell,
				candidates=candidates,
			)

			if matched_candidate is None:
				continue

			candidate_lookup[(cell.shelf_idx, cell.cell_idx)] = matched_candidate

	return candidate_lookup


def wall_depths_by_cell(
	*,
	analysis: Any,
	candidates: list[Any] | None = None,
) -> dict[tuple[int, int], float]:
	"""Wall depth per detected cell: the sweep candidate that measured it, or one the analysis carries.

	A grid-built analysis (a rack that moved since the last scan) supplies its own depths, because
	its positions come from stored calibration points rather than from a scan frame.
	"""
	lookup: dict[tuple[int, int], float] = {}

	if candidates:
		candidate_lookup = build_candidate_lookup_by_cell(analysis=analysis, candidates=candidates)

		for key, candidate in candidate_lookup.items():
			if candidate.wall_depth_mm is None:
				continue

			lookup[(int(key[0]), int(key[1]))] = float(candidate.wall_depth_mm)

	for key, wall_depth_mm in getattr(analysis, "wall_depths_by_cell", {}).items():
		if wall_depth_mm is not None and float(wall_depth_mm) > 0.0:
			lookup[(int(key[0]), int(key[1]))] = float(wall_depth_mm)

	return lookup


def apply_xy_reference_offsets(
	*,
	scanned_shelves: list[Any],
	scan_config: Any,
	side: StorageModuleSide,
) -> None:
	xy_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)

	if xy_offset is None:
		require_scan_xy_correction(scan_config=scan_config, side=side)
		return

	print_status(
		f"xy_offset_inference=fixed_offset offset_x_mm={xy_offset[0]:.3f} "
		f"offset_y_mm={xy_offset[1]:.3f}"
	)
	apply_xy_reference_delta(
		scanned_shelves=scanned_shelves,
		delta_x_mm=xy_offset[0],
		delta_y_mm=xy_offset[1],
	)


def get_existing_shelf_xy_delta(
	*,
	scanned_shelf: Any,
	target_shelf: Any,
	included_cell_keys: set[tuple[int, int]] | None = None,
	) -> tuple[float, float]:
	existing_lookup = {
		int(cell.cell_idx): cell
		for cell in getattr(target_shelf, "cells", [])
		if getattr(cell, "calibration_point", None) is not None
	}
	delta_x_values: list[float] = []
	delta_y_values: list[float] = []

	for scanned_cell in getattr(scanned_shelf, "cells", []):
		if scanned_cell.calibration_point is None:
			continue

		cell_key = (int(scanned_shelf.shelf_idx), int(scanned_cell.cell_idx))
		if included_cell_keys is not None and cell_key not in included_cell_keys:
			continue

		existing_cell = existing_lookup.get(int(scanned_cell.cell_idx))
		if existing_cell is None:
			continue

		delta_x_values.append(float(existing_cell.calibration_point.x) - float(scanned_cell.calibration_point.x))
		delta_y_values.append(float(existing_cell.calibration_point.y) - float(scanned_cell.calibration_point.y))

	if len(delta_x_values) == 0 or len(delta_y_values) == 0:
		raise ValueError("Cannot align partial shelf rescan without existing matching calibration cells")

	delta_x_mm = float(np.median(np.array(delta_x_values, dtype=np.float32)))
	delta_y_mm = float(np.median(np.array(delta_y_values, dtype=np.float32)))
	print_status(
		f"xy_offset_inference=existing_shelf delta_x_mm={delta_x_mm:.3f} "
		f"delta_y_mm={delta_y_mm:.3f} matched_cells={len(delta_x_values)}"
	)
	return delta_x_mm, delta_y_mm


def apply_xy_reference_delta(
	*,
	scanned_shelves: list[Any],
	delta_x_mm: float,
	delta_y_mm: float,
	included_cell_keys: set[tuple[int, int]] | None = None,
	) -> None:
	updated_cell_count = 0

	for shelf in scanned_shelves:
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			cell_key = (int(shelf.shelf_idx), int(cell.cell_idx))
			if included_cell_keys is not None and cell_key not in included_cell_keys:
				continue

			cell.calibration_point.x = float(cell.calibration_point.x) + float(delta_x_mm)
			cell.calibration_point.y = float(cell.calibration_point.y) + float(delta_y_mm)
			updated_cell_count += 1

	print_status(
		f"xy_offset_inference=applied delta_x_mm={float(delta_x_mm):.3f} "
		f"delta_y_mm={float(delta_y_mm):.3f} updated_cells={updated_cell_count}"
	)



async def estimate_seeded_candidate_wall_depths(
	tool: Any,
	*,
	bounds: ScanBounds,
	candidates: list[GapCandidateRegion],
	scan_config: Any,
	coarse_scan: GridScanResult,
	should_abort: Callable[[], bool] | None = None,
	on_flying_line: Optional[Callable[[str, Any], None]] = None,
) -> None:
	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, SHELF_SWEEP_X_STEP_MM)

	if len(coarse_scan.x_positions) == 0 or len(coarse_scan.y_positions) == 0:
		initialized_scan = create_sparse_shelf_scan(bounds=bounds)
		coarse_scan.x_positions = initialized_scan.x_positions
		coarse_scan.y_positions = initialized_scan.y_positions
		coarse_scan.depth_rows = initialized_scan.depth_rows

	for candidate_idx, candidate in enumerate(candidates, start=1):
		if candidate.wall_depth_mm is not None and float(candidate.wall_depth_mm) > 0.0:
			continue

		raise_if_cancelled(should_abort)
		scan_y_mm = float((candidate.y_min + candidate.y_max) / 2.0)
		progress_label = f"seeded_wall_depth_{candidate_idx}"

		depth_values, seeded_flying_line = await scan_line_positions_flying(
			tool,
			axis="x",
			positions=x_positions,
			fixed_mm=scan_y_mm,
			rpm=get_coarse_scan_rpm(scan_config),
			progress_label=progress_label,
			should_abort=should_abort,
			bin_mode=get_coarse_scan_bin_mode(scan_config),
			capture_latency_s=get_flying_capture_latency_s(scan_config),
		)

		if on_flying_line is not None:
			on_flying_line(f"{progress_label}_flying", seeded_flying_line)

		merge_line_scan_into_sparse_scan(
			coarse_scan,
			axis="x",
			positions=x_positions,
			fixed_mm=scan_y_mm,
			depth_values=depth_values,
		)
		wall_depth_mm, _ = find_gap_segments_in_line(
			values=depth_values,
			positions=x_positions,
			step_mm=SHELF_SWEEP_X_STEP_MM,
			scan_config=scan_config,
		)

		if wall_depth_mm <= 0.0:
			continue

		candidate.wall_depth_mm = float(wall_depth_mm)
		candidate.wall_sample_count = max(1, int(candidate.wall_sample_count))
		print_status(
			f"seeded_wall_depth candidate={candidate_idx}/{len(candidates)} "
			f"y={scan_y_mm:.2f} wall_depth_mm={wall_depth_mm:.2f}"
		)


Z_DELTA_MAX_ABS_MM = 25.0



def disable_unreachable_cells(
	*,
	scanned_shelves: list[Any],
	x_axis_limit_mm: float,
) -> int:
	"""Disable cells whose grab X lies beyond the X-axis travel limit.

	The laser sits ahead of the tool on the same carriage (by laser_offset_x), so the scan
	can detect holes the tool can never reach: grabbing one requires the carriage to travel
	that offset further than the laser did. Without this guard the axis silently clamps at
	its limit, the tool ends up misaligned by the overshoot, and the pick drives into the
	divider. Returns the number of cells disabled.
	"""
	disabled_count = 0

	for shelf in scanned_shelves:
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			grab_x_mm = float(cell.calibration_point.x)

			if grab_x_mm <= float(x_axis_limit_mm):
				continue

			cell.status = CellStatus.DISABLED
			disabled_count += 1
			print_status(
				f"cell_unreachable=disabled shelf={shelf.shelf_idx} cell={cell.cell_idx} "
				f"grab_x_mm={grab_x_mm:.1f} x_axis_limit_mm={float(x_axis_limit_mm):.1f}"
			)

	return disabled_count


def apply_wall_depth_z_offsets(
	*,
	analysis: Any,
	scanned_shelves: list[Any],
	metadata_path: str,
	z_delta_mm: float,
	z_delta_slope_per_mm: float = 0.0,
	detected_cell_key_map: dict[tuple[int, int], tuple[int, int]] | None = None,
	shelf_intercepts: list[tuple[float, float]] | None = None,
	cell_z_offsets: dict[tuple[int, int], float] | None = None,
	) -> None:
	"""`cell_z_offsets` (analysis keys) are z values measured on the cell itself, e.g. by its grip
	edge, and replace the model's z for those cells."""
	metadata_file = Path(metadata_path)

	if metadata_file.exists() is False:
		print_status("z_offset_inference=skipped reason=missing_metadata")
		return

	metadata = load_json_file(metadata_file)
	candidates = load_candidates_from_metadata(metadata)
	candidate_lookup = build_candidate_lookup_by_cell(
		analysis=analysis,
		candidates=candidates,
	)

	if len(candidate_lookup) == 0:
		raise ValueError(
			"z offset inference: no scanned cell has a laser wall depth, so every cell would be "
			"written with z_offset=0 and the gripper would not reach any tray"
		)

	updated_cell_count = 0
	measured_cell_count = 0
	preserved_cell_count = 0
	output_to_analysis_key: dict[tuple[int, int], tuple[int, int]] | None = None
	if detected_cell_key_map is not None:
		output_to_analysis_key = {}
		for analysis_key, output_key in detected_cell_key_map.items():
			if output_key in output_to_analysis_key:
				raise ValueError(f"Multiple detected cells mapped to scanned cell {output_key[0]}:{output_key[1]}")
			output_to_analysis_key[output_key] = analysis_key

	# wall_depth is the sole per-cell z source. A cell without one used to be skipped silently,
	# which left it at the z_offset=0 default from build_shelves_from_scan.
	missing_cells: list[str] = []

	for shelf in scanned_shelves:
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			output_key = (int(shelf.shelf_idx), int(cell.cell_idx))
			analysis_key = output_key
			if output_to_analysis_key is not None:
				mapped_analysis_key = output_to_analysis_key.get(output_key)
				if mapped_analysis_key is None:
					# This is an existing cell preserved because it was physically absent from
					# the partial scan. Its existing z_offset is intentional, not a blank scan.
					preserved_cell_count += 1
					continue
				analysis_key = mapped_analysis_key

			candidate = candidate_lookup.get(analysis_key)

			if candidate is None or candidate.wall_depth_mm is None:
				missing_cells.append(f"{output_key[0]}:{output_key[1]}")
				continue

			cell_intercept_mm = z_intercept_for_y(
				intercept_mm=float(z_delta_mm),
				shelf_intercepts=shelf_intercepts,
				y_mm=float(cell.calibration_point.y),
			)
			cell_delta_mm = cell_intercept_mm + float(z_delta_slope_per_mm) * float(cell.calibration_point.x)
			cell.calibration_point.z_offset = float(candidate.wall_depth_mm) + cell_delta_mm

			if cell_z_offsets is not None and analysis_key in cell_z_offsets:
				cell.calibration_point.z_offset = float(cell_z_offsets[analysis_key])
				measured_cell_count += 1

			updated_cell_count += 1

	if len(missing_cells) > 0:
		raise ValueError(
			f"z offset inference: no laser wall depth for {len(missing_cells)} scanned cell(s) "
			f"({', '.join(missing_cells[:8])}{'...' if len(missing_cells) > 8 else ''}). "
			"Those cells would keep z_offset=0; refusing to persist a partially blank grid."
		)

	print_status(
		f"z_offset_inference=applied intercept_mm={float(z_delta_mm):.3f} "
		f"slope_per_mm={float(z_delta_slope_per_mm):.5f} shelf_intercepts={len(shelf_intercepts or [])} "
		f"updated_cells={updated_cell_count} measured_cells={measured_cell_count} preserved_cells={preserved_cell_count}"
	)


def collect_detected_cells(analysis: Any) -> list[ScanDetectedCell]:
	collected_cells: list[ScanDetectedCell] = []

	for shelf in analysis.shelves:
		collected_cells.extend(shelf.cells)

	return collected_cells


def find_detected_cell_for_candidate(
	*,
	analysis: Any,
	candidate: GapCandidateRegion,
) -> ScanDetectedCell | None:
	detected_cells = collect_detected_cells(analysis)

	if len(detected_cells) == 0:
		return None

	candidate_center_x_mm, candidate_center_y_mm = get_candidate_center(candidate)
	matched_cell: ScanDetectedCell | None = None
	matched_distance_mm: float | None = None

	for detected_cell in detected_cells:

		distance_mm = float(
			np.hypot(
				float(detected_cell.center_x_mm) - candidate_center_x_mm,
				float(detected_cell.center_y_mm) - candidate_center_y_mm,
			)
		)

		if matched_distance_mm is not None and distance_mm >= matched_distance_mm:
			continue

		matched_cell = detected_cell
		matched_distance_mm = distance_mm

	return matched_cell


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


def convert_stored_y_mm_to_laser_frame(
	*,
	stored_y_mm: float,
	side: StorageModuleSide,
	scan_config: Any,
) -> float:
	"""Convert a stored calibration y into the laser frame the scan moves in.

	Stored calibration points already include the fixed laser-to-gripper reference correction,
	so a stored y is a gripper coordinate. Scan bounds and seeded shelf edges are laser
	coordinates; use this everywhere a stored y is turned into scan geometry.
	"""
	laser_offset_y_mm = require_scan_config_parameter(scan_config, "laser_offset_y_mm")
	xy_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)
	laser_y_mm = float(stored_y_mm) + float(laser_offset_y_mm)

	if xy_offset is not None:
		laser_y_mm -= float(xy_offset[1])

	return laser_y_mm


def build_single_shelf_known_edge_y(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	shelf_idx: int,
	scan_config: Any,
) -> list[float]:
	target_shelf = resolve_target_shelf(
		grid=grid,
		side=side,
		shelf_idx=shelf_idx,
	)
	candidate_center_y_values: list[float] = []

	for cell in target_shelf.cells:
		if cell.calibration_point is None:
			continue

		candidate_center_y_values.append(
			convert_stored_y_mm_to_laser_frame(
				stored_y_mm=float(cell.calibration_point.y),
				side=side,
				scan_config=scan_config,
			)
		)

	if len(candidate_center_y_values) == 0:
		raise ValueError(f"Shelf {shelf_idx} has no calibration points on {side.value}")

	hole_probe_y_offsets_mm = get_shelf_hole_probe_y_offsets_mm(scan_config)
	reference_offset_mm = float(hole_probe_y_offsets_mm[0])
	target_center_y_mm = float(np.median(np.array(candidate_center_y_values, dtype=np.float32)))

	return [target_center_y_mm - reference_offset_mm]


def merge_scanned_shelf(
	*,
	existing_shelves: list[Any],
	scanned_shelf: Any,
	target_shelf_idx: int,
) -> list[Any]:
	updated_shelves = list(existing_shelves)

	for shelf_position, shelf in enumerate(updated_shelves):
		if int(shelf.shelf_idx) != target_shelf_idx:
			continue

		updated_shelves[shelf_position] = scanned_shelf
		return updated_shelves

	raise ValueError(f"Shelf {target_shelf_idx} does not exist")


def apply_single_cell_rescan(
	*,
	grid: StorageGrid,
	target: SingleCellRescanTarget,
	side: StorageModuleSide,
	detected_cell: ScanDetectedCell,
	scan_config: Any,
	) -> None:
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

	calibration_point = target_cell.calibration_point
	xy_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)

	if getattr(target_cell, "is_manual", False) is True:
		# A hand-calibrated cell is the datum every derive/probe run is judged against, and this
		# rescan runs before the probe and saves the grid: rewriting its point from the configured
		# offset would corrupt the very value the manual calibration exists to correct, even when
		# the probe is subsequently refused.
		print_status(
			f"xy_offset_inference=skipped reason=single_cell_preserves_manual_datum "
			f"cell={target.shelf_idx}:{target.cell_idx}"
		)
	elif xy_offset is not None:
		print_status(
			f"xy_offset_inference=fixed_offset offset_x_mm={xy_offset[0]:.3f} "
			f"offset_y_mm={xy_offset[1]:.3f}"
		)
		calibration_point.x = float(detected_cell.tool_x_mm) + xy_offset[0]
		calibration_point.y = float(detected_cell.tool_y_mm) + xy_offset[1]
	else:
		print_status("xy_offset_inference=skipped reason=single_cell_preserves_existing_frame")

	if getattr(target_cell, "is_manual", False) is False:
		# As in a shelf rebuild (see _build_scan_cell), a recalibrated cell must be resized by hand.
		target_cell.dimensions = Dimensions(width=0.0, height=0.0, length=0.0)

	grid.save()
	print_status(
		f"single_cell_apply=updated side={side.value} "
		f"cell={target.shelf_idx}:{target.cell_idx} "
		f"tool_x={float(detected_cell.tool_x_mm):.3f} "
		f"tool_y={float(detected_cell.tool_y_mm):.3f} "
		f"z_offset={float(calibration_point.z_offset):.3f}"
	)


def apply_single_shelf_analysis(
	*,
	analysis: Any,
	grid: StorageGrid,
	grid_path: Path,
	scan_config: Any,
	side: StorageModuleSide,
	target_shelf_idx: int,
	z_delta_override_mm: float | None = None,
	z_delta_slope_per_mm: float = 0.0,
	shelf_intercepts: list[tuple[float, float]] | None = None,
	cell_z_offsets: dict[tuple[int, int], float] | None = None,
	) -> None:
	if len(analysis.shelves) == 0:
		raise ValueError("No tray openings detected from scan")

	if len(analysis.shelves) != 1:
		raise ValueError("Expected exactly one shelf from single-shelf calibration")

	target_shelf = resolve_target_shelf(
		grid=grid,
		side=side,
		shelf_idx=target_shelf_idx,
	)
	remapped_existing_shelf = target_shelf.model_copy(
		deep=True,
		update={"shelf_idx": 0},
	)
	detected_cell_key_map: dict[tuple[int, int], tuple[int, int]] = {}
	scanned_shelves = build_shelves_from_scan(
		side=side,
		analysis=analysis,
		existing_shelves=[remapped_existing_shelf],
		detected_cell_key_map=detected_cell_key_map,
	)

	if len(scanned_shelves) != 1:
		raise ValueError("Expected exactly one scanned shelf for single-shelf calibration")

	scanned_shelf = scanned_shelves[0]
	scanned_shelf.divider_width = float(target_shelf.divider_width)
	# Manual cells keep the operator's point (see _build_scan_cell): leave them out of the x/y and
	# z rewrites below, so the datum a later probe is judged against stays a hand-set value.
	manual_cell_keys = {
		(int(shelf.shelf_idx), int(cell.cell_idx))
		for shelf in [target_shelf, remapped_existing_shelf]
		for cell in shelf.cells
		if getattr(cell, "is_manual", False) is True
	}
	manual_cell_keys |= {
		(0, cell_idx) for shelf_idx, cell_idx in manual_cell_keys if shelf_idx == target_shelf_idx
	}


	def is_manual_key(key: tuple[int, int]) -> bool:
		# Only ever called with an OUTPUT key: `detected_cell_key_map` is keyed by the detected
		# indices while the rebuilt cells carry the stored ones, so testing the analysis key here
		# would drop an unrelated opening's rewrite - silently, as a "preserved" manual cell.
		return (int(key[0]), int(key[1])) in manual_cell_keys
	detected_output_keys = (
		{
			output_key
			for output_key in detected_cell_key_map.values()
			if is_manual_key(output_key) is False
		}
		if len(detected_cell_key_map) > 0
		else None
	)

	xy_offset = get_gripper_xy_correction(scan_config=scan_config, side=side)

	if xy_offset is not None:
		print_status(
			f"xy_offset_inference=fixed_offset offset_x_mm={xy_offset[0]:.3f} "
			f"offset_y_mm={xy_offset[1]:.3f}"
		)
		apply_xy_reference_delta(
			scanned_shelves=[scanned_shelf],
			delta_x_mm=xy_offset[0],
			delta_y_mm=xy_offset[1],
			included_cell_keys=detected_output_keys,
		)
	else:
		delta_x_mm, delta_y_mm = get_existing_shelf_xy_delta(
			scanned_shelf=scanned_shelf,
			target_shelf=target_shelf,
			included_cell_keys=detected_output_keys,
		)
		apply_xy_reference_delta(
			scanned_shelves=[scanned_shelf],
			delta_x_mm=delta_x_mm,
			delta_y_mm=delta_y_mm,
			included_cell_keys=detected_output_keys,
		)
	if z_delta_override_mm is not None:
		z_apply_kwargs: dict[str, Any] = {}
		z_apply_map = {
			source_key: target_key
			for source_key, target_key in detected_cell_key_map.items()
			if is_manual_key(target_key) is False
		}
		# Keyed on the unfiltered map: passing an empty map is meaningful, because it tells
		# `apply_wall_depth_z_offsets` that every cell it can see is preserved. Omitting it would
		# fall back to analysis-keyed cells and rewrite the manual datum it is meant to leave alone.
		if len(detected_cell_key_map) > 0:
			z_apply_kwargs["detected_cell_key_map"] = z_apply_map

		apply_wall_depth_z_offsets(
			analysis=analysis,
			scanned_shelves=[scanned_shelf],
			metadata_path=str(analysis.metadata_path),
			z_delta_mm=z_delta_override_mm,
			z_delta_slope_per_mm=z_delta_slope_per_mm,
			shelf_intercepts=shelf_intercepts,
			cell_z_offsets=cell_z_offsets,
			**z_apply_kwargs,
		)
	else:
		print_status("z_offset_inference=skipped reason=partial_rescan_preserves_existing_z_offsets")

	# Re-stamp to the target index only after the Z apply. apply_wall_depth_z_offsets keys
	# its candidate lookup by the analysis shelf_idx (single-shelf analysis is re-indexed to
	# 0), so the scanned shelf must still be shelf 0 while z_offsets are written; it then
	# moves to its real slot for the merge.
	scanned_shelf.shelf_idx = target_shelf_idx

	front_module = list(grid.front_module)
	back_module = list(grid.back_module)
	merged_shelves = merge_scanned_shelf(
		existing_shelves=grid.get_side(side),
		scanned_shelf=scanned_shelf,
		target_shelf_idx=target_shelf_idx,
	)

	if side == StorageModuleSide.FRONT:
		front_module = merged_shelves
	else:
		back_module = merged_shelves

	storage_grid = StorageGrid(
		front_module=front_module,
		back_module=back_module,
		filename=grid.filename if grid.filename != "" else str(grid_path),
	)
	storage_grid.save()



