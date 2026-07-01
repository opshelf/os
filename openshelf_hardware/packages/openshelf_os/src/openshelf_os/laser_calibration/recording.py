# ruff: noqa: F401, F403, F405
from __future__ import annotations

from .core import *

def get_coarse_settle_ms(fine_settle_ms: int) -> int:
    return max(0, fine_settle_ms // 2)


def get_coarse_samples_per_point(fine_samples_per_point: int) -> int:
    return 1

def build_run_parameters(
	*,
	args: argparse.Namespace,
	side: StorageModuleSide,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	coarse_settle_ms: int,
	coarse_samples_per_point: int,
	scan_config: Any,
) -> dict[str, Any]:
	validate_scan_plan(
		bounds=bounds,
		step_mm=step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		should_validate_point_count=False,
	)
	validate_laser_structure_parameters(
		scan_config=scan_config,
		side=side,
	)
	xy_offset = get_scan_xy_offset(scan_config=scan_config, side=side)
	hole_probe_y_offsets_mm = get_shelf_hole_probe_y_offsets_mm(scan_config)
	laser_scan_stage_extension_mm = get_laser_scan_stage_extension_mm(scan_config)
	laser_scan_z_offset_from_place_mm = get_laser_scan_z_offset_from_place_mm(scan_config)
	laser_offset_x_mm, laser_offset_y_mm = require_laser_offsets(scan_config)
	post_edge_skip_mm = get_shelf_scout_post_edge_skip_mm(scan_config)
	brim_max_height_mm = get_shelf_brim_max_height_mm(scan_config)

	return {
		"module": side.value,
		"cli": {
			"module": args.module,
			"x": (
				[float(args.x[0]), float(args.x[1])]
				if args.x is not None
				else None
			),
			"y": (
				[float(args.y[0]), float(args.y[1])]
				if args.y is not None
				else None
			),
			"y_offset": float(args.y_offset),
			"step": step_mm,
			"settle_ms": args.settle_ms,
			"samples_per_point": args.samples_per_point,
			"known_shelf_edge_y": (
				[float(value) for value in args.known_shelf_edge_y]
				if args.known_shelf_edge_y is not None
				else None
			),
			"npy": str(args.npy) if args.npy is not None else None,
			"scans_dir": args.scans_dir,
			"config": args.config,
			"apply": args.apply,
			"shelf_scout_only": args.shelf_scout_only,
			"rescan_cell": args.rescan_cell,
		},
		"resolved": {
			"bounds": bounds.model_dump(),
			"fine_step_mm": step_mm,
			"coarse_step_mm": COARSE_SCAN_STEP_MM,
			"shelf_scout_step_mm": SHELF_SCOUT_STEP_MM,
			"shelf_scout_post_edge_skip_mm": post_edge_skip_mm,
			"shelf_brim_max_height_mm": brim_max_height_mm,
			"shelf_sweep_x_step_mm": SHELF_SWEEP_X_STEP_MM,
			"shelf_hole_probe_y_offsets_mm": list(hole_probe_y_offsets_mm),
			"settle_ms": settle_ms,
			"samples_per_point": samples_per_point,
			"coarse_settle_ms": coarse_settle_ms,
			"coarse_samples_per_point": coarse_samples_per_point,
			"laser_scan_stage_extension_mm": laser_scan_stage_extension_mm,
			"laser_scan_z_offset_from_place_mm": laser_scan_z_offset_from_place_mm,
			"laser_offset_x_mm": laser_offset_x_mm,
			"laser_offset_y_mm": laser_offset_y_mm,
			"xy_offset": (
				{
					"x_mm": float(xy_offset[0]),
					"y_mm": float(xy_offset[1]),
				}
				if xy_offset is not None
				else None
			),
			"laser_scan_y_offset_mm": float(args.y_offset),
			"shelf_edge_top_margin_mm": SHELF_EDGE_TOP_MARGIN_MM,
		},
	}


def persist_run_parameters(
	*,
	run_dir: Path,
	run_id: str,
	run_parameters: dict[str, Any],
) -> str:
	run_parameters_path = run_dir / "run_parameters.json"
	run_parameters_path.write_text(
		json.dumps(
			{
				"run_id": run_id,
				"created_at": get_current_timestamp(),
				"run_parameters": run_parameters,
			},
			indent="\t",
		),
		encoding="utf-8",
	)

	return str(run_parameters_path)


def save_depth_rows_to_npy(
	*,
	output_path: Path,
	depth_rows: list[list[float | None]],
) -> None:
	depth_array = np.array(
		[
			[np.nan if depth_mm is None else float(depth_mm) for depth_mm in row]
			for row in depth_rows
		],
		dtype=np.float32,
	)
	np.save(output_path, depth_array)


def create_staged_scan_recorder(
	*,
	scans_dir: str,
	run_parameters: dict[str, Any],
) -> StagedScanRecorder:
	run_id = uuid4().hex
	run_dir = Path(scans_dir) / run_id
	run_dir.mkdir(parents=True, exist_ok=True)

	recorder = StagedScanRecorder(
		run_id=run_id,
		run_dir=run_dir,
		coarse_depth_path=run_dir / "coarse_depth.npy",
		metadata_path=run_dir / "metadata.json",
		run_parameters_path=run_dir / "run_parameters.json",
		progress_path=run_dir / "progress.json",
		log_path=run_dir / "scan.log",
	)

	recorder.log_path.write_text("", encoding="utf-8")
	persist_run_parameters(
		run_dir=run_dir,
		run_id=run_id,
		run_parameters=run_parameters,
	)
	return recorder


def load_staged_scan_recorder(run_dir: Path) -> StagedScanRecorder:
	return StagedScanRecorder(
		run_id=run_dir.name,
		run_dir=run_dir,
		coarse_depth_path=run_dir / "coarse_depth.npy",
		metadata_path=run_dir / "metadata.json",
		run_parameters_path=run_dir / "run_parameters.json",
		progress_path=run_dir / "progress.json",
		log_path=run_dir / "scan.log",
	)


def find_latest_incomplete_run(scans_dir: str) -> StagedScanRecorder:
	run_dirs: list[Path] = []

	for path in Path(scans_dir).iterdir():
		try:
			if path.is_dir() is False:
				continue

			if (path / "run_parameters.json").exists() is False:
				continue
		except PermissionError:
			continue

		run_dirs.append(path)

	run_dirs = sorted(
		run_dirs,
		key=lambda current_path: current_path.stat().st_mtime,
		reverse=True,
	)

	for run_dir in run_dirs:
		recorder = load_staged_scan_recorder(run_dir)

		if recorder.metadata_path.exists() is False:
			return recorder

		metadata = load_json_file(recorder.metadata_path)

		if metadata.get("status") != "completed":
			return recorder

	raise FileNotFoundError(f"No incomplete staged scan found in {scans_dir}")


def load_continue_scan_context(
	scans_dir: str,
) -> tuple[StagedScanRecorder, StorageModuleSide, ScanBounds, float, int, int, int, int]:
	recorder = find_latest_incomplete_run(scans_dir)
	run_parameters_document = load_json_file(recorder.run_parameters_path)
	run_parameters = run_parameters_document["run_parameters"]
	resolved_parameters = run_parameters["resolved"]
	side = StorageModuleSide(run_parameters["module"])
	bounds = ScanBounds.model_validate(resolved_parameters["bounds"])
	step_mm = float(resolved_parameters["fine_step_mm"])
	settle_ms = int(resolved_parameters["settle_ms"])
	samples_per_point = int(resolved_parameters["samples_per_point"])
	coarse_settle_ms = int(
		resolved_parameters.get("coarse_settle_ms", get_coarse_settle_ms(settle_ms))
	)
	coarse_samples_per_point = int(
		resolved_parameters.get(
			"coarse_samples_per_point",
			get_coarse_samples_per_point(samples_per_point),
		)
	)

	return (
		recorder,
		side,
		bounds,
		step_mm,
		settle_ms,
		samples_per_point,
		coarse_settle_ms,
		coarse_samples_per_point,
	)


def load_json_file(path: Path) -> dict[str, Any]:
	return json.loads(path.read_text(encoding="utf-8"))


def load_candidates_from_metadata(metadata: dict[str, Any]) -> list[GapCandidateRegion]:
	return [
		GapCandidateRegion(
			x_min=float(entry["x_min"]),
			x_max=float(entry["x_max"]),
			y_min=float(entry["y_min"]),
			y_max=float(entry["y_max"]),
			row_count=int(entry.get("row_count", 1)),
			wall_depth_mm=(
				float(entry["wall_depth_mm"])
				if entry.get("wall_depth_mm") is not None
				else None
			),
			wall_sample_count=int(entry.get("wall_sample_count", 0)),
		)
		for entry in metadata.get("candidates", [])
	]


def load_partial_grid_scan(
	*,
	npy_path: Path,
	bounds: ScanBounds,
	step_mm: float,
) -> GridScanResult:
	depth_array = np.load(npy_path)

	if depth_array.ndim != 2:
		raise ValueError(f"Expected 2D array at {npy_path}")

	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)
	row_count, column_count = depth_array.shape

	if column_count != len(x_positions):
		raise ValueError(
			f"Saved scan width does not match bounds for {npy_path} "
			f"(expected {len(x_positions)}, got {column_count})"
		)

	if row_count > len(y_positions):
		raise ValueError(
			f"Saved scan height exceeds bounds for {npy_path} "
			f"(expected at most {len(y_positions)}, got {row_count})"
		)

	depth_rows: list[list[float | None]] = []

	for row in depth_array:
		depth_row: list[float | None] = []

		for value in row:
			float_value = float(value)

			if bool(np.isfinite(float_value)) is True:
				depth_row.append(float_value)
				continue

			depth_row.append(None)

		depth_rows.append(depth_row)

	return GridScanResult(
		x_positions=x_positions,
		y_positions=y_positions[:row_count],
		depth_rows=depth_rows,
	)


def persist_scan_progress(
	*,
	recorder: StagedScanRecorder,
	phase: str,
	progress: dict[str, Any],
) -> None:
	recorder.progress_path.write_text(
		json.dumps(
			{
				"run_id": recorder.run_id,
				"updated_at": get_current_timestamp(),
				"phase": phase,
				"progress": progress,
			},
			indent="\t",
		),
		encoding="utf-8",
	)


def persist_staged_metadata(
	*,
	recorder: StagedScanRecorder,
	side: StorageModuleSide,
	bounds: ScanBounds,
	fine_step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	coarse_settle_ms: int,
	coarse_samples_per_point: int,
	wall_depth_mm: float,
	coarse_scan: GridScanResult,
	candidates: list[GapCandidateRegion],
	fine_entries: list[dict[str, Any]],
	status: str,
	scan_started_at: Optional[str] = None,
	shelf_edges: Optional[list[ShelfSurfaceEdge]] = None,
) -> None:
	metadata_document: dict[str, Any] = {
		"run_id": recorder.run_id,
		"created_at": get_current_timestamp(),
		"status": status,
		"side": side.value,
		"bounds": bounds.model_dump(),
		"fine_step_mm": fine_step_mm,
		"coarse_step_mm": COARSE_SCAN_STEP_MM,
		"settle_ms": settle_ms,
		"samples_per_point": samples_per_point,
		"coarse_settle_ms": coarse_settle_ms,
		"coarse_samples_per_point": coarse_samples_per_point,
		"wall_depth_mm": wall_depth_mm,
		"coarse_scan": {
			"raw_scan_path": str(recorder.coarse_depth_path),
			"x_positions": coarse_scan.x_positions,
			"y_positions": coarse_scan.y_positions,
		},
		"candidates": [
			{
				"x_min": candidate.x_min,
				"x_max": candidate.x_max,
				"y_min": candidate.y_min,
				"y_max": candidate.y_max,
				"row_count": candidate.row_count,
				"wall_depth_mm": candidate.wall_depth_mm,
				"wall_sample_count": candidate.wall_sample_count,
			}
			for candidate in candidates
		],
		"fine_scans": fine_entries,
		"shelf_edges": [
			{
				"x_mm": edge.x_mm,
				"y_mm": edge.y_mm,
			}
			for edge in (shelf_edges or [])
		],
	}

	if scan_started_at is not None:
		metadata_document["scan_started_at"] = scan_started_at

	recorder.metadata_path.write_text(
		json.dumps(metadata_document, indent="\t"),
		encoding="utf-8",
	)


def persist_scan_started_at(
	*,
	recorder: StagedScanRecorder,
	scan_started_at: str,
) -> None:
	if recorder.run_parameters_path.exists() is True:
		run_parameters_document = load_json_file(recorder.run_parameters_path)
		run_parameters_document["scan_started_at"] = scan_started_at
		recorder.run_parameters_path.write_text(
			json.dumps(run_parameters_document, indent="\t"),
			encoding="utf-8",
		)

	if recorder.metadata_path.exists() is False:
		return

	metadata_document = load_json_file(recorder.metadata_path)
	metadata_document["scan_started_at"] = scan_started_at
	recorder.metadata_path.write_text(
		json.dumps(metadata_document, indent="\t"),
		encoding="utf-8",
	)


def load_depth_rows_from_npy(
	npy_path: Path,
	*,
	expected_row_count: int,
	expected_column_count: int,
) -> list[list[float | None]]:
	depth_array = np.load(npy_path)

	if depth_array.ndim != 2:
		raise ValueError("--npy must point to a 2D array")

	row_count, column_count = depth_array.shape

	if row_count != expected_row_count or column_count != expected_column_count:
		raise ValueError(
			"--npy shape does not match scan bounds "
			f"(expected {expected_row_count}x{expected_column_count}, got {row_count}x{column_count})"
		)

	depth_rows: list[list[float | None]] = []

	for row in depth_array:
		depth_row: list[float | None] = []

		for value in row:
			float_value = float(value)

			if bool(np.isfinite(float_value)) is True:
				depth_row.append(float_value)
				continue

			depth_row.append(None)

		depth_rows.append(depth_row)

	return depth_rows
