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
		should_validate_reference_bounds=seeded_candidates is None,
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
	xy_reference = get_scan_xy_reference(scan_config=scan_config, side=side)

	anchor_cell = get_scanned_anchor_cell(
		scanned_shelves=scanned_shelves,
		side=side,
		anchor=str(xy_reference.anchor),
	)

	if anchor_cell is None or anchor_cell.calibration_point is None:
		print_status("xy_offset_inference=skipped reason=anchor_unresolved")
		return

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


def resolve_z_reference_cell(
	*,
	scan_config: Any,
	side: StorageModuleSide,
	) -> tuple[int, int, float, float] | None:
	return get_scan_z_reference(scan_config=scan_config, side=side)


def apply_wall_depth_z_offsets(
	*,
	analysis: Any,
	scanned_shelves: list[Any],
	metadata_path: str,
	scan_config: Any,
	side: StorageModuleSide,
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
	z_delta = get_scan_z_delta(
		scan_config=scan_config,
		side=side,
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
		f"reference_wall_depth_mm={reference_key[3]:.3f} "
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
) -> None:
	target_cell = resolve_target_cell(
		grid=grid,
		side=side,
		target=target,
	)
	calibration_point = target_cell.calibration_point

	calibration_point.x = float(detected_cell.tool_x_mm)
	calibration_point.y = float(detected_cell.tool_y_mm)
	z_delta = get_scan_z_delta(
		scan_config=scan_config,
		side=side,
	)
	calibration_point.z_offset = float(wall_depth_mm) + float(z_delta["delta_mm"])

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
	apply_wall_depth_z_offsets(
		analysis=analysis,
		scanned_shelves=[scanned_shelf],
		metadata_path=str(analysis.metadata_path),
		scan_config=scan_config,
		side=side,
	)
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
