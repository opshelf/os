# ruff: noqa: F401, F403, F405
from __future__ import annotations

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
	should_validate_reference_bounds: bool = True,
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
		bounds=bounds,
		should_validate_reference_bounds=should_validate_reference_bounds and seeded_candidates is None,
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
					await estimate_seeded_candidate_wall_depths(
						tool,
						bounds=bounds,
						candidates=candidates,
						settle_ms=coarse_settle_ms,
						samples_per_point=coarse_samples_per_point,
						scan_config=scan_config,
						coarse_scan=coarse_scan,
						should_abort=should_abort,
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
		wall_depth_tolerance_mm=scan_config.wall_depth_tolerance_mm,
		expected_hole_width_mm=scan_config.expected_hole_width_mm,
		expected_hole_height_mm=scan_config.expected_hole_height_mm,
		min_vertical_wall_coverage_mm=scan_config.min_vertical_wall_coverage_mm,
		min_horizontal_wall_coverage_mm=scan_config.min_horizontal_wall_coverage_mm,
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


def get_scanned_anchor_cell(
	*,
	scanned_shelves: list[Any],
	side: StorageModuleSide,
	anchor: str,
) -> Any | None:
	if len(scanned_shelves) == 0:
		return None

	ordered_shelves = sorted(scanned_shelves, key=lambda shelf: float(shelf.shelf_idx))

	if anchor.startswith("top_"):
		target_shelf = ordered_shelves[-1]
	else:
		target_shelf = ordered_shelves[0]

	if len(target_shelf.cells) == 0:
		return None

	is_right_anchor = anchor.endswith("right")

	if is_right_anchor is True:
		return max(target_shelf.cells, key=lambda cell: float(cell.calibration_point.x))

	return min(target_shelf.cells, key=lambda cell: float(cell.calibration_point.x))


def apply_xy_reference_offsets(
	*,
	scanned_shelves: list[Any],
	scan_config: Any,
	side: StorageModuleSide,
) -> None:
	xy_reference = get_scan_xy_reference_optional(scan_config=scan_config, side=side)

	if xy_reference is None:
		xy_offset = get_scan_xy_offset(scan_config=scan_config, side=side)

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
		return

	anchor_cell = get_scanned_anchor_cell(
		scanned_shelves=scanned_shelves,
		side=side,
		anchor=str(xy_reference.anchor),
	)

	if anchor_cell is None or anchor_cell.calibration_point is None:
		print_status("xy_offset_inference=skipped reason=anchor_unresolved")
		return

	anchor_shelf_idx = getattr(anchor_cell, "shelf_idx", None)
	for shelf in scanned_shelves:
		if any(cell is anchor_cell for cell in shelf.cells):
			anchor_shelf_idx = shelf.shelf_idx
			break

	if int(anchor_shelf_idx) != int(xy_reference.shelf_idx) or int(getattr(anchor_cell, "cell_idx", -1)) != int(xy_reference.cell_idx):
		raise ValueError(
			"Detected xy reference anchor "
			f"{anchor_shelf_idx}:{getattr(anchor_cell, 'cell_idx', None)} "
			"does not match configured reference target "
			f"{int(xy_reference.shelf_idx)}:{int(xy_reference.cell_idx)}"
		)

	delta_x_mm = float(xy_reference.x_mm) - float(anchor_cell.calibration_point.x)
	delta_y_mm = float(xy_reference.y_mm) - float(anchor_cell.calibration_point.y)
	updated_cell_count = 0

	for shelf in scanned_shelves:
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			cell.calibration_point.x = float(cell.calibration_point.x) + delta_x_mm
			cell.calibration_point.y = float(cell.calibration_point.y) + delta_y_mm
			updated_cell_count += 1

	print_status(
		f"xy_offset_inference=applied anchor={xy_reference.anchor} "
		f"reference_x_mm={float(xy_reference.x_mm):.3f} "
		f"reference_y_mm={float(xy_reference.y_mm):.3f} "
		f"delta_x_mm={delta_x_mm:.3f} delta_y_mm={delta_y_mm:.3f} updated_cells={updated_cell_count}"
	)


def get_xy_reference_delta(
	*,
	detected_cell: ScanDetectedCell,
	scan_config: Any,
	side: StorageModuleSide,
	) -> tuple[float, float]:
	xy_reference = get_scan_xy_reference(scan_config=scan_config, side=side)
	delta_x_mm = float(xy_reference.x_mm) - float(detected_cell.tool_x_mm)
	delta_y_mm = float(xy_reference.y_mm) - float(detected_cell.tool_y_mm)

	print_status(
		f"xy_offset_inference=reference_scan anchor={xy_reference.anchor} "
		f"reference_x_mm={float(xy_reference.x_mm):.3f} "
		f"reference_y_mm={float(xy_reference.y_mm):.3f} "
		f"detected_x_mm={float(detected_cell.tool_x_mm):.3f} "
		f"detected_y_mm={float(detected_cell.tool_y_mm):.3f} "
		f"delta_x_mm={delta_x_mm:.3f} delta_y_mm={delta_y_mm:.3f}"
	)

	return delta_x_mm, delta_y_mm


def get_existing_shelf_xy_delta(
	*,
	scanned_shelf: Any,
	target_shelf: Any,
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
	) -> None:
	updated_cell_count = 0

	for shelf in scanned_shelves:
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			cell.calibration_point.x = float(cell.calibration_point.x) + float(delta_x_mm)
			cell.calibration_point.y = float(cell.calibration_point.y) + float(delta_y_mm)
			updated_cell_count += 1

	print_status(
		f"xy_offset_inference=applied delta_x_mm={float(delta_x_mm):.3f} "
		f"delta_y_mm={float(delta_y_mm):.3f} updated_cells={updated_cell_count}"
	)



def resolve_z_reference_cell(
	*,
	scan_config: Any,
	side: StorageModuleSide,
	) -> tuple[int, int, float, float]:
	return get_scan_z_reference(scan_config=scan_config, side=side)


async def estimate_seeded_candidate_wall_depths(
	tool: Any,
	*,
	bounds: ScanBounds,
	candidates: list[GapCandidateRegion],
	settle_ms: int,
	samples_per_point: int,
	scan_config: Any,
	coarse_scan: GridScanResult,
	should_abort: Callable[[], bool] | None = None,
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
		depth_values = await scan_line_positions(
			tool,
			axis="x",
			positions=x_positions,
			fixed_mm=scan_y_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			progress_label=f"seeded_wall_depth_{candidate_idx}",
			should_abort=should_abort,
		)
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


def apply_wall_depth_z_offsets(
	*,
	analysis: Any,
	scanned_shelves: list[Any],
	metadata_path: str,
	scan_config: Any,
	side: StorageModuleSide,
	current_stage_extension_mm: float = 43.0,
	z_reference_wall_depth_mm: float | None = None,
	should_use_analysis_wall_depth_fallback: bool = True,
	) -> None:
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
		print_status("z_offset_inference=skipped reason=no_candidate_wall_depth")
		return

	reference_key = resolve_z_reference_cell(
		scan_config=scan_config,
		side=side,
	)
	reference_candidate = candidate_lookup.get((reference_key[0], reference_key[1]))
	reference_wall_depth_mm: float | None = None

	if z_reference_wall_depth_mm is not None:
		reference_wall_depth_mm = float(z_reference_wall_depth_mm)
	elif reference_candidate is not None and reference_candidate.wall_depth_mm is not None:
		reference_wall_depth_mm = float(reference_candidate.wall_depth_mm)
	elif should_use_analysis_wall_depth_fallback is False and reference_candidate is None:
		raise ValueError(
			"Detected cells do not include configured z reference target "
			f"{reference_key[0]}:{reference_key[1]}"
		)
	elif should_use_analysis_wall_depth_fallback is True and getattr(analysis, "wall_depth_mm", 0.0) is not None and float(analysis.wall_depth_mm) > 0.0:
		reference_wall_depth_mm = float(analysis.wall_depth_mm)
	if reference_wall_depth_mm is None:
		raise ValueError("Cannot resolve z reference wall depth from scan results")

	z_delta = get_scan_z_delta(
		scan_config=scan_config,
		side=side,
		reference_wall_depth_mm=reference_wall_depth_mm,
		current_stage_extension_mm=current_stage_extension_mm,
	)

	z_delta_mm = float(z_delta["delta_mm"])
	updated_cell_count = 0

	for shelf in scanned_shelves:
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			candidate = candidate_lookup.get((shelf.shelf_idx, cell.cell_idx))

			if candidate is None or candidate.wall_depth_mm is None:
				continue

			cell.calibration_point.z_offset = float(candidate.wall_depth_mm) + z_delta_mm
			updated_cell_count += 1

	print_status(
		f"z_offset_inference=applied reference={reference_key[0]}:{reference_key[1]} "
		f"reference_z_offset_mm={reference_key[2]:.3f} "
		f"reference_stage_extension_mm={reference_key[3]:.3f} "
		f"current_stage_extension_mm={float(current_stage_extension_mm):.3f} "
		f"reference_wall_depth_mm={reference_wall_depth_mm:.3f} "
		f"delta_mm={z_delta_mm:.3f} updated_cells={updated_cell_count}"
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
	laser_offset_y_mm = require_scan_config_parameter(scan_config, "laser_offset_y_mm")
	candidate_center_y_values: list[float] = []

	for cell in target_shelf.cells:
		if cell.calibration_point is None:
			continue

		candidate_center_y_values.append(
			float(cell.calibration_point.y) + laser_offset_y_mm
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
	wall_depth_mm: float,
	scan_config: Any,
	current_stage_extension_mm: float = 43.0,
	xy_reference_detected_cell: ScanDetectedCell | None = None,
	z_reference_wall_depth_mm: float | None = None,
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
	previous_z_offset = float(calibration_point.z_offset)
	calibration_x = float(detected_cell.tool_x_mm)
	calibration_y = float(detected_cell.tool_y_mm)

	if xy_reference_detected_cell is not None:
		delta_x_mm, delta_y_mm = get_xy_reference_delta(
			detected_cell=xy_reference_detected_cell,
			scan_config=scan_config,
			side=side,
		)
		calibration_x += delta_x_mm
		calibration_y += delta_y_mm
	else:
		print_status("xy_offset_inference=skipped reason=single_cell_preserves_existing_frame")
		calibration_x = float(calibration_point.x)
		calibration_y = float(calibration_point.y)


	calibration_point.x = calibration_x
	calibration_point.y = calibration_y

	if z_reference_wall_depth_mm is not None:
		z_delta = get_scan_z_delta(
			scan_config=scan_config,
			side=side,
			reference_wall_depth_mm=float(z_reference_wall_depth_mm),
			current_stage_extension_mm=current_stage_extension_mm,
		)
		calibration_point.z_offset = float(wall_depth_mm) + float(z_delta["delta_mm"])
	else:
		calibration_point.z_offset = previous_z_offset

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
	current_stage_extension_mm: float = 43.0,
	xy_reference_detected_cell: ScanDetectedCell | None = None,
	z_reference_wall_depth_mm: float | None = None,
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
	scanned_shelves = build_shelves_from_scan(
		side=side,
		analysis=analysis,
		existing_shelves=[remapped_existing_shelf],
	)

	if len(scanned_shelves) != 1:
		raise ValueError("Expected exactly one scanned shelf for single-shelf calibration")

	scanned_shelf = scanned_shelves[0]
	scanned_shelf.shelf_idx = target_shelf_idx
	scanned_shelf.divider_width = float(target_shelf.divider_width)

	if xy_reference_detected_cell is not None:
		delta_x_mm, delta_y_mm = get_xy_reference_delta(
			detected_cell=xy_reference_detected_cell,
			scan_config=scan_config,
			side=side,
		)
		apply_xy_reference_delta(
			scanned_shelves=[scanned_shelf],
			delta_x_mm=delta_x_mm,
			delta_y_mm=delta_y_mm,
		)
	else:
		delta_x_mm, delta_y_mm = get_existing_shelf_xy_delta(
			scanned_shelf=scanned_shelf,
			target_shelf=target_shelf,
		)
		apply_xy_reference_delta(
			scanned_shelves=[scanned_shelf],
			delta_x_mm=delta_x_mm,
			delta_y_mm=delta_y_mm,
		)
	if z_reference_wall_depth_mm is not None:
		apply_wall_depth_z_offsets(
			analysis=analysis,
			scanned_shelves=[scanned_shelf],
			metadata_path=str(analysis.metadata_path),
			scan_config=scan_config,
			side=side,
			current_stage_extension_mm=current_stage_extension_mm,
			z_reference_wall_depth_mm=z_reference_wall_depth_mm,
			should_use_analysis_wall_depth_fallback=False,
		)
	else:
		print_status("z_offset_inference=skipped reason=partial_rescan_preserves_existing_z_offsets")
	left_module = list(grid.left_module)
	right_module = list(grid.right_module)
	merged_shelves = merge_scanned_shelf(
		existing_shelves=grid.get_side(side),
		scanned_shelf=scanned_shelf,
		target_shelf_idx=target_shelf_idx,
	)

	if side == StorageModuleSide.LEFT:
		left_module = merged_shelves
	else:
		right_module = merged_shelves

	storage_grid = StorageGrid(
		left_module=left_module,
		right_module=right_module,
		filename=grid.filename if grid.filename != "" else str(grid_path),
	)
	storage_grid.save()


def apply_single_cell_shelf_analysis(
	*,
	analysis: Any,
	grid: StorageGrid,
	grid_path: Path,
	scan_config: Any,
	side: StorageModuleSide,
	target: SingleCellRescanTarget,
	) -> None:
	if len(analysis.shelves) == 0:
		raise ValueError("No tray openings detected from scan")

	if len(analysis.shelves) != 1:
		raise ValueError("Expected exactly one shelf from single-cell calibration")

	target_shelf = resolve_target_shelf(
		grid=grid,
		side=side,
		shelf_idx=target.shelf_idx,
	)
	target_cell = resolve_target_cell(
		grid=grid,
		side=side,
		target=target,
	)
	remapped_existing_shelf = target_shelf.model_copy(
		deep=True,
		update={"shelf_idx": 0},
	)
	scanned_shelves = build_shelves_from_scan(
		side=side,
		analysis=analysis,
		existing_shelves=[remapped_existing_shelf],
	)

	if len(scanned_shelves) != 1:
		raise ValueError("Expected exactly one scanned shelf for single-cell calibration")

	scanned_shelf = scanned_shelves[0]
	scanned_shelf.shelf_idx = target.shelf_idx
	delta_x_mm, delta_y_mm = get_existing_shelf_xy_delta(
		scanned_shelf=scanned_shelf,
		target_shelf=target_shelf,
	)
	apply_xy_reference_delta(
		scanned_shelves=[scanned_shelf],
		delta_x_mm=delta_x_mm,
		delta_y_mm=delta_y_mm,
	)

	try:
		scanned_target_cell = scanned_shelf.get_cell(target.cell_idx)
	except IndexError as exc:
		raise ValueError(
			f"No detected cell matched the single-cell calibration target {target.shelf_idx}:{target.cell_idx}"
		) from exc

	if scanned_target_cell.calibration_point is None:
		raise ValueError("Detected single-cell calibration target has no calibration point")

	target_cell.calibration_point.x = float(scanned_target_cell.calibration_point.x)
	target_cell.calibration_point.y = float(scanned_target_cell.calibration_point.y)
	grid.save()
	print_status(
		f"single_cell_shelf_apply=updated side={side.value} "
		f"cell={target.shelf_idx}:{target.cell_idx} "
		f"x={float(target_cell.calibration_point.x):.3f} "
		f"y={float(target_cell.calibration_point.y):.3f} "
		f"z_offset={float(target_cell.calibration_point.z_offset):.3f}"
	)


def resolve_validation_coordinate(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	target: SingleCellRescanTarget,
) -> AxisCoordinate:
	target_cell = resolve_target_cell(
		grid=grid,
		side=side,
		target=target,
	)

	return AxisCoordinate(
		x=float(target_cell.calibration_point.x),
		y=float(target_cell.calibration_point.y),
		s=0 if side == StorageModuleSide.LEFT else 1,
		z_offset=float(target_cell.calibration_point.z_offset),
	)


async def run_fixed_xy_validation(
	*,
	robot: Robot,
	grid: StorageGrid,
	side: StorageModuleSide,
	target: SingleCellRescanTarget,
	cycles: int,
) -> None:
	if cycles <= 0:
		raise ValueError("--validate-cycles must be greater than zero")

	coordinate = resolve_validation_coordinate(
		grid=grid,
		side=side,
		target=target,
	)
	tool = robot._tool
	print_status(
		f"validation_target side={side.value} cell={target.shelf_idx}:{target.cell_idx} "
		f"x={coordinate.x:.3f} y={coordinate.y:.3f} z_offset={float(coordinate.z_offset):.3f} "
		f"cycles={cycles}"
	)

	for cycle_idx in range(cycles):

		current_cycle_idx = cycle_idx + 1

		print_status(
			f"validation_cycle={current_cycle_idx}/{cycles} phase=pick"
		)
		await tool.pick(coordinate)
		print_status(
			f"validation_cycle={current_cycle_idx}/{cycles} phase=place"
		)
		await tool.place(coordinate)

	print_status("validation_status=completed")


def apply_analysis(
	*,
	analysis: Any,
	grid: StorageGrid,
	grid_path: Path,
	scan_config: Any,
	side: StorageModuleSide,
	current_stage_extension_mm: float = 43.0,
) -> None:
	if len(analysis.shelves) == 0:
		raise ValueError("No tray openings detected from scan")

	existing_shelves = grid.get_side(side)
	scanned_shelves = build_shelves_from_scan(
		side=side,
		analysis=analysis,
		existing_shelves=existing_shelves,
	)
	apply_xy_reference_offsets(
		scanned_shelves=scanned_shelves,
		scan_config=scan_config,
		side=side,
	)
	apply_wall_depth_z_offsets(
		analysis=analysis,
		scanned_shelves=scanned_shelves,
		metadata_path=str(analysis.metadata_path),
		scan_config=scan_config,
		side=side,
		current_stage_extension_mm=current_stage_extension_mm,
		should_use_analysis_wall_depth_fallback=False,
	)
	left_module = list(grid.left_module)
	right_module = list(grid.right_module)

	if side == StorageModuleSide.LEFT:
		left_module = scanned_shelves
	else:
		right_module = scanned_shelves

	storage_grid = StorageGrid(
		left_module=left_module,
		right_module=right_module,
		filename=grid.filename if grid.filename != "" else str(grid_path),
	)
	storage_grid.save()


def resolve_staged_run_dir(path: Path | str) -> Path:
    run_path = Path(path)

    if run_path.is_file() is True:
        if run_path.name != "metadata.json":
            raise ValueError("staged run file must be metadata.json")

        return run_path.parent

    return run_path


def resolve_saved_raw_scan_path(
    *,
    run_dir: Path,
    raw_scan_path: str,
    label: str,
    require_exists: bool,
) -> Path:
    saved_path = Path(raw_scan_path)

    if saved_path.is_absolute() is False:
        resolved_path = run_dir / saved_path

        if resolved_path.exists() is True or require_exists is False:
            return resolved_path

        raise FileNotFoundError(f"Saved {label} raw scan file not found at {resolved_path}")

    if saved_path.exists() is True:
        return saved_path

    local_path = run_dir / saved_path.name

    if local_path.exists() is True:
        return local_path

    if require_exists is False:
        return saved_path

    raise FileNotFoundError(
        f"Saved {label} raw scan file not found at {saved_path} or {local_path}"
    )


def find_replay_gap_segment(
	*,
	values: list[float | None],
	positions: list[float],
	step_mm: float,
	wall_depth_mm: float,
	expected_width_mm: float,
	width_tolerance_mm: float,
	center_mm: float,
	scan_config: Any,
) -> LineGapSegment | None:
	if len(values) != len(positions):
		raise ValueError("values and positions must have the same length")

	matched_segments: list[LineGapSegment] = []
	start_idx: int | None = None

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

		segment = build_line_run_segment(
			positions=positions,
			start_idx=start_idx,
			end_idx=end_idx,
			step_mm=step_mm,
		)

		if dimensions_match(
			segment.width_mm,
			expected_mm=expected_width_mm,
			tolerance_mm=width_tolerance_mm,
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

		matched_segments.append(segment)
		start_idx = None

	if len(matched_segments) == 0:
		return None

	return min(
		matched_segments,
		key=lambda segment: abs(float(segment.center_mm) - float(center_mm)),
	)


def build_detected_cell_from_saved_fine_scan(
	*,
	fine_scan: GridScanResult,
	candidate: GapCandidateRegion,
	fine_step_mm: float,
	wall_depth_mm: float,
	scan_config: Any,
) -> ScanDetectedCell | None:
	if wall_depth_mm <= 0.0:
		return None

	candidate_center_x_mm, candidate_center_y_mm = get_candidate_center(candidate)
	horizontal_segments: list[LineGapSegment] = []

	for row in fine_scan.depth_rows:
		if any(value is not None for value in row) is False:
			continue

		segment = find_replay_gap_segment(
			values=row,
			positions=fine_scan.x_positions,
			step_mm=fine_step_mm,
			wall_depth_mm=wall_depth_mm,
			expected_width_mm=scan_config.expected_hole_width_mm,
			width_tolerance_mm=scan_config.hole_width_tolerance_mm + fine_step_mm,
			center_mm=candidate_center_x_mm,
			scan_config=scan_config,
		)

		if segment is not None:
			horizontal_segments.append(segment)

	if len(horizontal_segments) == 0:
		return None

	horizontal_segment = min(
		horizontal_segments,
		key=lambda segment: abs(float(segment.center_mm) - candidate_center_x_mm),
	)
	vertical_segments: list[LineGapSegment] = []

	for col_idx, _ in enumerate(fine_scan.x_positions):
		column_values = [row[col_idx] for row in fine_scan.depth_rows]

		if any(value is not None for value in column_values) is False:
			continue

		segment = find_replay_gap_segment(
			values=column_values,
			positions=fine_scan.y_positions,
			step_mm=fine_step_mm,
			wall_depth_mm=wall_depth_mm,
			expected_width_mm=scan_config.expected_hole_height_mm,
			width_tolerance_mm=scan_config.hole_height_tolerance_mm + fine_step_mm,
			center_mm=candidate_center_y_mm,
			scan_config=scan_config,
		)

		if segment is None:
			continue

		vertical_segments.append(segment)

	if len(vertical_segments) == 0:
		return None

	vertical_segment = min(
		vertical_segments,
		key=lambda segment: abs(float(segment.center_mm) - candidate_center_y_mm),
	)
	return build_detected_cell_from_segments(
		x_segment=horizontal_segment,
		y_segment=vertical_segment,
		step_mm=fine_step_mm,
		scan_config=scan_config,
	)


def load_staged_scan_analysis(
    path: Path | str,
    *,
    scan_config: Any,
    require_completed: bool = True,
) -> ScanAnalysisResult:
    run_dir = resolve_staged_run_dir(path)
    metadata_path = run_dir / "metadata.json"

    if metadata_path.exists() is False:
        raise FileNotFoundError(f"No staged scan metadata found at {metadata_path}")

    metadata = load_json_file(metadata_path)

    if require_completed is True and metadata.get("status") != "completed":
        raise ValueError(f"Staged scan {run_dir} is not completed")

    side = StorageModuleSide(metadata["side"])
    bounds = ScanBounds.model_validate(metadata["bounds"])
    fine_step_mm = float(metadata["fine_step_mm"])
    candidates = load_candidates_from_metadata(metadata)
    detected_cells: list[ScanDetectedCell] = []

    for fine_entry_idx, fine_entry in enumerate(metadata.get("fine_scans", [])):
        if fine_entry.get("status") != "completed":
            continue

        raw_fine_path_value = str(fine_entry.get("raw_scan_path", ""))

        if raw_fine_path_value == "":
            continue

        raw_fine_path = resolve_saved_raw_scan_path(
            run_dir=run_dir,
            raw_scan_path=raw_fine_path_value,
            label="fine",
            require_exists=True,
        )

        candidate_index = int(fine_entry.get("candidate_index", fine_entry_idx))

        if candidate_index < 0 or candidate_index >= len(candidates):
            continue

        candidate = candidates[candidate_index]
        fine_bounds = ScanBounds.model_validate(fine_entry["bounds"])
        fine_scan = load_partial_grid_scan(
            npy_path=raw_fine_path,
            bounds=fine_bounds,
            step_mm=fine_step_mm,
        )
        candidate_wall_depth_mm = float(metadata.get("wall_depth_mm", 0.0))

        if candidate.wall_depth_mm is not None:
            candidate_wall_depth_mm = float(candidate.wall_depth_mm)

        detected_cell = build_detected_cell_from_saved_fine_scan(
            fine_scan=fine_scan,
            candidate=candidate,
            fine_step_mm=fine_step_mm,
            wall_depth_mm=candidate_wall_depth_mm,
            scan_config=scan_config,
        )

        if detected_cell is not None:
            detected_cells.append(detected_cell)

    deduped_cells = dedupe_detected_cells(
        detected_cells,
        scan_config=scan_config,
    )
    shelves = build_detected_shelves(
        deduped_cells,
        side=side,
        shelf_threshold_mm=get_shelf_grouping_threshold_mm(
            step_mm=fine_step_mm,
            scan_config=scan_config,
        ),
    )
    raw_scan_path = str(metadata.get("coarse_scan", {}).get("raw_scan_path", run_dir / "coarse_depth.npy"))
    coarse_raw_path = resolve_saved_raw_scan_path(
        run_dir=run_dir,
        raw_scan_path=raw_scan_path,
        label="coarse",
        require_exists=False,
    )

    raw_scan_path = str(coarse_raw_path)
    created_at = datetime.now(timezone.utc)

    if metadata.get("created_at") is not None:
        created_at = datetime.fromisoformat(str(metadata["created_at"]))

    total_points = 0
    valid_points = 0

    if coarse_raw_path.exists() is True:
        coarse_scan = load_partial_grid_scan(
            npy_path=coarse_raw_path,
            bounds=bounds,
            step_mm=float(metadata.get("coarse_step_mm", COARSE_SCAN_STEP_MM)),
        )
        total_points += count_valid_points(coarse_scan.depth_rows)
        valid_points += count_valid_points(coarse_scan.depth_rows)

    for fine_entry in metadata.get("fine_scans", []):
        raw_fine_path_value = str(fine_entry.get("raw_scan_path", ""))

        if raw_fine_path_value == "":
            continue

        if fine_entry.get("status") != "completed":
            continue

        raw_fine_path = resolve_saved_raw_scan_path(
            run_dir=run_dir,
            raw_scan_path=raw_fine_path_value,
            label="fine",
            require_exists=True,
        )

        fine_bounds = ScanBounds.model_validate(fine_entry["bounds"])
        fine_scan = load_partial_grid_scan(
            npy_path=raw_fine_path,
            bounds=fine_bounds,
            step_mm=fine_step_mm,
        )
        total_points += count_valid_points(fine_scan.depth_rows)
        valid_points += count_valid_points(fine_scan.depth_rows)

    return ScanAnalysisResult(
        run_id=str(metadata.get("run_id", run_dir.name)),
        side=side,
        bounds=bounds,
        step_mm=fine_step_mm,
        samples_per_point=int(metadata["samples_per_point"]),
        settle_ms=int(metadata["settle_ms"]),
        wall_depth_mm=float(metadata.get("wall_depth_mm", 0.0)),
        wall_depth_tolerance_mm=float(scan_config.wall_depth_tolerance_mm),
        expected_hole_width_mm=float(scan_config.expected_hole_width_mm),
        expected_hole_height_mm=float(scan_config.expected_hole_height_mm),
        min_vertical_wall_coverage_mm=float(scan_config.min_vertical_wall_coverage_mm),
        min_horizontal_wall_coverage_mm=float(scan_config.min_horizontal_wall_coverage_mm),
        total_points=total_points,
        valid_points=valid_points,
        raw_scan_path=raw_scan_path,
        metadata_path=str(metadata_path),
        shelves=shelves,
        created_at=created_at,
    )


def analyze_depth_rows(
	*,
	side: StorageModuleSide,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	scan_config: Any,
	scans_dir: str,
	depth_rows: list[list[float | None]],
) -> Any:
	laser_offset_x_mm, laser_offset_y_mm = require_laser_offsets(scan_config)
	capture = create_scan_capture(
		side=side,
		bounds=bounds,
		step_mm=step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		laser_offset_x_mm=laser_offset_x_mm,
		laser_offset_y_mm=laser_offset_y_mm,
		wall_depth_tolerance_mm=scan_config.wall_depth_tolerance_mm,
		expected_hole_width_mm=scan_config.expected_hole_width_mm,
		expected_hole_height_mm=scan_config.expected_hole_height_mm,
		hole_width_tolerance_mm=scan_config.hole_width_tolerance_mm,
		hole_height_tolerance_mm=scan_config.hole_height_tolerance_mm,
		min_vertical_wall_coverage_mm=scan_config.min_vertical_wall_coverage_mm,
		min_horizontal_wall_coverage_mm=scan_config.min_horizontal_wall_coverage_mm,
		depth_rows=depth_rows,
	)

	return analyze_scan_capture(capture, base_dir=scans_dir)
