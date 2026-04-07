#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import numpy as np
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
	REPO_ROOT / "perpetual_hardware/packages/perpetual_os1/src",
	REPO_ROOT / "perpetual_hardware/packages/hardware/src",
]

for import_path in reversed(PYTHON_PATHS):
	sys.path.insert(0, str(import_path))

from perpetual_os1.common import AxisCoordinate, CONFIG_PATH, load_config_file, resolve_grid_path
from perpetual_os1.laser_scan import (
	ScanBounds,
	ScanAnalysisResult,
	ScanDetectedCell,
	ScanDetectedShelf,
	analyze_scan_capture,
	build_axis_positions,
	build_shelves_from_scan,
	create_scan_capture,
)
import perpetual_os1.common as common_module
import perpetual_os1.robot as robot_module
from perpetual_os1.robot import Robot
from perpetual_os1.storage import StorageGrid, StorageModuleSide

LASER_SCAN_STAGE_EXTENSION_MM = 13.0
LASER_SCAN_Z_OFFSET_MM = 10.0
COARSE_SCAN_STEP_MM = 4.0
SHELF_SCOUT_STEP_MM = 2.0
SHELF_SCOUT_X_OFFSETS_MM = [0.0]
RIGHT_MODULE_ROTATION_SAFE_X_MM = 475.0
SHELF_SWEEP_X_STEP_MM = 4.0
SHELF_SWEEP_MIN_CANDIDATE_WIDTH_MM = 20.0
SHELF_EDGE_CONFIRMATION_COUNT = 3
SHELF_BRIM_MIN_HEIGHT_MM = 8.0
SHELF_BRIM_MAX_HEIGHT_MM = 14.0
SHELF_MIN_SPACING_MM = 40.0
SHELF_EDGE_TOP_MARGIN_MM = 5.0
SHELF_HOLE_SWEEP_OFFSETS_MM = [32.25]
SHELF_SCOUT_POST_EDGE_SKIP_MM = 80.0
GAP_DEPTH_THRESHOLD_MM = 0.01
FILLED_HOLE_MIN_DEPTH_DELTA_MM = 0.4
FINE_SCAN_MARGIN_X_MM = 10.0
FINE_SCAN_MARGIN_Y_MM = 6.0
FINE_OPPOSITE_EDGE_MARGIN_MM = 5.0
FINE_OPPOSITE_EDGE_BACKTRACK_MM = 8.0
COARSE_WALL_SEARCH_STEPS = 2
CURRENT_SCAN_LOG_PATH: Optional[Path] = None
DEFAULT_Z_REFERENCE_BY_SIDE: dict[StorageModuleSide, tuple[int, int, float, float]] = {
	StorageModuleSide.LEFT: (12, 0, 15.0, 8.504),
}


def get_scan_stage_extension_mm(scan_config: Any) -> float:
	configured_value = getattr(scan_config, "scan_stage_extension_mm", None)

	if configured_value is None:
		return LASER_SCAN_STAGE_EXTENSION_MM

	return float(configured_value)


def get_scan_z_offset_mm(scan_config: Any) -> float:
	configured_value = getattr(scan_config, "scan_z_offset_mm", None)

	if configured_value is None:
		return LASER_SCAN_Z_OFFSET_MM

	return float(configured_value)


def get_scan_z_reference(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> tuple[int, int, float, float] | None:
	configured_reference = None

	if side == StorageModuleSide.LEFT:
		configured_reference = getattr(scan_config, "left_z_reference", None)
	else:
		configured_reference = getattr(scan_config, "right_z_reference", None)

	if configured_reference is not None:
		return (
			int(configured_reference.shelf_idx),
			int(configured_reference.cell_idx),
			float(configured_reference.z_offset_mm),
			float(configured_reference.wall_depth_mm),
		)

	return DEFAULT_Z_REFERENCE_BY_SIDE.get(side)


def get_scan_z_delta(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> dict[str, Any] | None:
	configured_delta = None

	if side == StorageModuleSide.LEFT:
		configured_delta = getattr(scan_config, "left_z_delta_mm", None)
	else:
		configured_delta = getattr(scan_config, "right_z_delta_mm", None)

	if configured_delta is not None:
		return {
			"delta_mm": float(configured_delta),
			"source": "configured_delta",
			"reference": None,
		}

	reference = get_scan_z_reference(scan_config=scan_config, side=side)

	if reference is None:
		return None

	return {
		"delta_mm": float(reference[2]) - float(reference[3]),
		"source": "reference",
		"reference": reference,
	}


def get_scan_xy_reference(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> Any | None:
	if side == StorageModuleSide.LEFT:
		return getattr(scan_config, "left_xy_reference", None)

	return getattr(scan_config, "right_xy_reference", None)


@dataclass
class GridScanResult:
	x_positions: list[float]
	y_positions: list[float]
	depth_rows: list[list[float | None]]


@dataclass
class GapCandidateRegion:
	x_min: float
	x_max: float
	y_min: float
	y_max: float
	row_count: int = 1
	wall_depth_mm: float | None = None
	wall_sample_count: int = 0


@dataclass
class ShelfSurfaceEdge:
	x_mm: float
	y_mm: float


@dataclass
class LineGapSegment:
	start_idx: int
	end_idx: int
	start_mm: float
	end_mm: float
	width_mm: float
	center_mm: float


@dataclass
class StagedScanRecorder:
	run_id: str
	run_dir: Path
	coarse_depth_path: Path
	metadata_path: Path
	run_parameters_path: Path
	progress_path: Path
	log_path: Path


def get_current_timestamp() -> str:
	return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run a laser tray scan and print detected calibration points.")
	parser.add_argument("--module", choices=["left", "right"])
	parser.add_argument("--x", nargs=2, metavar=("MIN", "MAX"), type=float)
	parser.add_argument("--y", nargs=2, metavar=("MIN", "MAX"), type=float)
	parser.add_argument("--y-offset", type=float, default=0.0)
	parser.add_argument("--step", type=float)
	parser.add_argument("--settle-ms", type=int)
	parser.add_argument("--samples-per-point", type=int)
	parser.add_argument("--known-shelf-edge-y", nargs="+", type=float)
	parser.add_argument("--npy", type=Path)
	parser.add_argument("--scans-dir", default="/tmp")
	parser.add_argument("--config", default=str(CONFIG_PATH))
	parser.add_argument("--apply", action="store_true")
	parser.add_argument("--continue", dest="continue_scan", action="store_true")
	parser.add_argument("--shelf-scout-only", action="store_true")
	return parser.parse_args()


def normalize_module(raw_module: str) -> StorageModuleSide:
	if raw_module == "left":
		return StorageModuleSide.LEFT

	return StorageModuleSide.RIGHT


def derive_bounds(args: argparse.Namespace) -> ScanBounds:
	x_min, x_max = args.x
	y_min, y_max = args.y

	if x_max < x_min:
		raise ValueError("--x requires MIN <= MAX")

	if y_max < y_min:
		raise ValueError("--y requires MIN <= MAX")

	return ScanBounds(
		x_min=float(x_min),
		x_max=float(x_max),
		y_min=float(y_min) + float(args.y_offset),
		y_max=float(y_max) + float(args.y_offset),
	)


def depth_to_label(depth_mm: float | None) -> str:
	if depth_mm is None:
		return "none"

	return f"{depth_mm:.2f}"


def is_gap_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm <= GAP_DEPTH_THRESHOLD_MM


def is_positive_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm > GAP_DEPTH_THRESHOLD_MM


def is_filled_hole_depth(
	depth_mm: float | None,
	*,
	wall_depth_mm: float,
) -> bool:
	if is_positive_depth(depth_mm) is False:
		return False

	return float(depth_mm) >= wall_depth_mm + FILLED_HOLE_MIN_DEPTH_DELTA_MM


def is_hole_interior_depth(
	depth_mm: float | None,
	*,
	wall_depth_mm: float,
) -> bool:
	if is_gap_depth(depth_mm) is True:
		return True

	return is_filled_hole_depth(
		depth_mm,
		wall_depth_mm=wall_depth_mm,
	)


def is_wall_like_depth(
	depth_mm: float | None,
	*,
	wall_depth_mm: float,
	tolerance_mm: float,
) -> bool:
	if is_positive_depth(depth_mm) is False:
		return False

	return abs(float(depth_mm) - wall_depth_mm) <= tolerance_mm


def estimate_wall_depth_mm(depth_rows: list[list[float | None]]) -> float:
	valid_values: list[float] = []

	for row in depth_rows:
		for depth_mm in row:
			if is_positive_depth(depth_mm) is False:
				continue

			valid_values.append(float(depth_mm))

	if len(valid_values) == 0:
		raise ValueError("No positive laser readings captured")

	valid_array = np.array(valid_values, dtype=np.float32)
	bin_size_mm = 0.5
	min_depth_mm = float(np.floor(np.min(valid_array) / bin_size_mm) * bin_size_mm)
	max_depth_mm = float(np.ceil(np.max(valid_array) / bin_size_mm) * bin_size_mm) + bin_size_mm
	bins = np.arange(min_depth_mm, max_depth_mm + bin_size_mm, bin_size_mm, dtype=np.float32)

	if bins.size < 2:
		return float(np.median(valid_array))

	histogram, edges = np.histogram(valid_array, bins=bins)
	dominant_idx = int(np.argmax(histogram))
	lower_bound_mm = float(edges[dominant_idx])
	upper_bound_mm = float(edges[dominant_idx + 1])
	dominant_values = valid_array[
		(valid_array >= lower_bound_mm) &
		(valid_array < upper_bound_mm)
	]

	if dominant_values.size == 0:
		return float(np.median(valid_array))

	return float(np.median(dominant_values))


def count_valid_points(depth_rows: list[list[float | None]]) -> int:
	count = 0

	for row in depth_rows:
		for depth_mm in row:
			if depth_mm is None:
				continue

			count += 1

	return count


def print_analysis(analysis) -> None:
	print(f"run_id: {analysis.run_id}")
	print(f"side: {analysis.side.value}")
	print(
		"bounds: "
		f"x=({analysis.bounds.x_min:.2f}, {analysis.bounds.x_max:.2f}) "
		f"y=({analysis.bounds.y_min:.2f}, {analysis.bounds.y_max:.2f})"
	)
	print(f"wall_depth_mm: {analysis.wall_depth_mm:.2f}")
	print(f"raw_scan_path: {analysis.raw_scan_path}")

	total_cells = sum(len(shelf.cells) for shelf in analysis.shelves)
	print(f"detected_shelves: {len(analysis.shelves)}")
	print(f"detected_cells: {total_cells}")

	if total_cells == 0:
		print("no calibration points found")
		return

	for shelf in analysis.shelves:
		for cell in shelf.cells:
			print(
				f"shelf={cell.shelf_idx} "
				f"cell={cell.cell_idx} "
				f"tool_x={cell.tool_x_mm:.2f} "
				f"tool_y={cell.tool_y_mm:.2f} "
				f"center_x={cell.center_x_mm:.2f} "
				f"center_y={cell.center_y_mm:.2f} "
				f"width={cell.width_mm:.2f} "
				f"height={cell.height_mm:.2f}"
				)


def print_status(message: str) -> None:
	if CURRENT_SCAN_LOG_PATH is not None:
		timestamp = datetime.now(timezone.utc).isoformat()
		with CURRENT_SCAN_LOG_PATH.open("a", encoding="utf-8") as log_file:
			log_file.write(f"{timestamp}\t{message}\n")

	print(message, flush=True)


def build_standalone_config(config) -> dict:
	config_data = config.model_dump(mode="json")
	config_data["robot"]["io"]["curtain"]["enabled"] = False

	tool_config = config_data["robot"]["tool"]
	tool_config.setdefault("laser", {})
	tool_config["laser"]["enabled"] = True

	robot_bus = config_data["robot"].setdefault("bus", {})
	laser_bus = robot_bus.get("laser")
	scale_bus = robot_bus.get("scale")
	scale_enabled = config_data["robot"]["io"].get("scale", {}).get("enabled", True)

	if laser_bus is None and scale_enabled is False and scale_bus is not None:
		robot_bus["laser"] = dict(scale_bus)
		print_status("config=using scale bus for laser")

	return config_data


def load_grid(grid_path: Path) -> StorageGrid:
	if grid_path.exists() is False:
		return StorageGrid(filename=str(grid_path))

	return StorageGrid.load_from_file(str(grid_path))


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
	z_reference = get_scan_z_reference(scan_config=scan_config, side=side)
	z_delta = get_scan_z_delta(scan_config=scan_config, side=side)
	xy_reference = get_scan_xy_reference(scan_config=scan_config, side=side)
	scan_stage_extension_mm = get_scan_stage_extension_mm(scan_config)
	scan_z_offset_mm = get_scan_z_offset_mm(scan_config)

	return {
		"module": side.value,
		"cli": {
			"module": args.module,
			"x": [float(args.x[0]), float(args.x[1])],
			"y": [float(args.y[0]), float(args.y[1])],
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
		},
		"resolved": {
			"bounds": bounds.model_dump(),
			"fine_step_mm": step_mm,
			"coarse_step_mm": COARSE_SCAN_STEP_MM,
			"shelf_scout_step_mm": SHELF_SCOUT_STEP_MM,
			"shelf_sweep_x_step_mm": SHELF_SWEEP_X_STEP_MM,
			"shelf_hole_sweep_offsets_mm": list(SHELF_HOLE_SWEEP_OFFSETS_MM),
			"settle_ms": settle_ms,
			"samples_per_point": samples_per_point,
			"coarse_settle_ms": coarse_settle_ms,
			"coarse_samples_per_point": coarse_samples_per_point,
			"laser_scan_stage_extension_mm": scan_stage_extension_mm,
			"laser_scan_z_offset_mm": scan_z_offset_mm,
			"z_reference": (
				{
					"shelf_idx": z_reference[0],
					"cell_idx": z_reference[1],
					"z_offset_mm": z_reference[2],
					"wall_depth_mm": z_reference[3],
				}
				if z_reference is not None
				else None
			),
			"z_delta_mm": None if z_delta is None else float(z_delta["delta_mm"]),
			"z_delta_source": None if z_delta is None else str(z_delta["source"]),
			"xy_reference": (
				{
					"anchor": str(xy_reference.anchor),
					"x_mm": float(xy_reference.x_mm),
					"y_mm": float(xy_reference.y_mm),
				}
				if xy_reference is not None
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


def load_json_file(path: Path) -> dict[str, Any]:
	return json.loads(path.read_text(encoding="utf-8"))


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
		key=lambda path: path.stat().st_mtime,
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

			if np.isfinite(float_value) is True:
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

			if np.isfinite(float_value) is True:
				depth_row.append(float_value)
				continue

			depth_row.append(None)

		depth_rows.append(depth_row)

	return depth_rows


async def get_scan_position(tool: Any) -> tuple[float, float]:
	scan_position = await tool.get_param("SCAN_POSITION")

	if scan_position is None:
		raise ValueError("SCAN_POSITION parameter is not configured")

	return float(scan_position[0]), float(scan_position[1])


async def get_laser_scan_z_extension_mm(tool: Any, scan_config: Any) -> float:
	z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")

	if z_extension_place is None:
		raise ValueError("Z_EXTENSION_PLACE parameter is not configured")

	return float(z_extension_place) + get_scan_z_offset_mm(scan_config)


async def ensure_scan_tool_ready(tool: Any) -> None:
	if tool._laser_enabled is False or tool._laser is None:
		raise ValueError("Laser is not enabled")

	if tool._mx is None or tool._my is None:
		raise ValueError("Gantry motors (x_axis, y_axis) are required for laser scan")

	if tool._ms is None or tool._mg is None:
		raise ValueError("Tool motors (stage, z_axis) are required for laser scan")


async def enter_scan_mode(tool: Any, side: StorageModuleSide, scan_config: Any) -> None:
	await ensure_scan_tool_ready(tool)

	side_number = 0
	target_position = "front"

	if side == StorageModuleSide.RIGHT:
		side_number = 1
		target_position = "back"

	stage_extension_mm = get_scan_stage_extension_mm(scan_config)
	z_extension_mm = await get_laser_scan_z_extension_mm(tool, scan_config)
	z_move_rpm = await tool.get_param("Z_MOVE_RPM")
	z_move_accel = await tool.get_param("Z_MOVE_ACCEL")
	rotation_front = await tool.get_param("ROTATION_FRONT")
	rotation_back = await tool.get_param("ROTATION_BACK")

	if z_move_rpm is None or z_move_accel is None:
		raise ValueError("Z_MOVE_RPM and Z_MOVE_ACCEL parameters are required for laser scan")

	scan_x_mm, scan_y_mm = await get_scan_position(tool)

	print_status(
		f"scan_setup=module:{side.value} "
		f"scan_position:({scan_x_mm:.2f},{scan_y_mm:.2f}) "
		f"stage_extension_mm:{stage_extension_mm:.2f} "
		f"z_extension_mm:{z_extension_mm:.2f} "
		f"rotation_front:{rotation_front} "
		f"rotation_back:{rotation_back}"
	)

	await tool.retract()

	if side == StorageModuleSide.RIGHT:
		await tool.move(
			AxisCoordinate(x=RIGHT_MODULE_ROTATION_SAFE_X_MM, y=scan_y_mm, s=side_number)
		)

		rotation_before = await tool._mr.get_position()
		print_status(f"rotation_before={rotation_before}")
		await tool.rotate_to_position(target_position)
		rotation_after = await tool._mr.get_position()
		print_status(f"rotation_after={rotation_after}")
		await tool.move(AxisCoordinate(x=scan_x_mm, y=scan_y_mm, s=side_number))
	else:
		await tool.move(AxisCoordinate(x=scan_x_mm, y=scan_y_mm, s=side_number))

		rotation_before = await tool._mr.get_position()
		print_status(f"rotation_before={rotation_before}")
		await tool.rotate_to_position(target_position)
		rotation_after = await tool._mr.get_position()
		print_status(f"rotation_after={rotation_after}")
	await tool._ms.ready()
	await tool._ms.move_to_position(stage_extension_mm)
	await tool._mg.move_to_position(z_extension_mm, rpm=z_move_rpm, accel=z_move_accel)


async def sample_laser_depth(
	tool: Any,
	*,
	settle_ms: int,
	samples_per_point: int,
) -> float | None:
	if settle_ms > 0:
		await asyncio.sleep(settle_ms / 1000.0)

	sample_values: list[float] = []

	for _ in range(samples_per_point):
		depth_mm = await tool.get_laser_distance()

		if depth_mm is None:
			continue

		sample_values.append(float(depth_mm))

	if len(sample_values) == 0:
		return None

	sample_values.sort()
	return sample_values[len(sample_values) // 2]


def build_shelf_scout_x_positions(
	*,
	bounds: ScanBounds,
) -> list[float]:
	positions: list[float] = []

	for offset_mm in SHELF_SCOUT_X_OFFSETS_MM:
		x_mm = max(bounds.x_min, bounds.x_max - offset_mm)

		if x_mm in positions:
			continue

		positions.append(x_mm)

	return positions


async def capture_shelf_candidates(
	tool: Any,
	*,
	bounds: ScanBounds,
	settle_ms: int,
	samples_per_point: int,
	scan_config: Any,
	recorder: StagedScanRecorder,
	shelf_scout_only: bool,
	known_shelf_edge_y: Optional[list[float]],
) -> tuple[GridScanResult, float, list[GapCandidateRegion], list[ShelfSurfaceEdge]]:
	coarse_scan = create_sparse_shelf_scan(bounds=bounds)
	scout_x_positions = build_shelf_scout_x_positions(bounds=bounds)
	scout_y_positions = list(reversed(build_axis_positions(bounds.y_min, bounds.y_max, SHELF_SCOUT_STEP_MM)))
	hole_sweep_x_positions = build_axis_positions(bounds.x_min, bounds.x_max, SHELF_SWEEP_X_STEP_MM)
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

	async def set_y_brake_for_scouting(
		*,
		is_enabled: bool,
	) -> None:
		y_motor = getattr(tool, "_my", None)

		if y_motor is None:
			raise ValueError("Y-axis motor is required for shelf scouting")

		if is_enabled is True:
			await y_motor.restore_brake_output()
			print_status("y_brake=enabled")
			return

		await y_motor.disable_brake_output()
		print_status("y_brake=disabled")

	if known_shelf_edge_y is not None:
		shelf_edges = [
			ShelfSurfaceEdge(
				x_mm=bounds.x_max,
				y_mm=float(y_mm),
			)
			for y_mm in known_shelf_edge_y
		]
	else:
		is_y_brake_disabled = False

		try:
			await set_y_brake_for_scouting(is_enabled=False)
			is_y_brake_disabled = True

			for scout_idx, scout_x_mm in enumerate(scout_x_positions, start=1):
				print_status(f"phase=shelf-scout scout={scout_idx}/{len(scout_x_positions)} x={scout_x_mm:.2f}")

				scout_depth_values, scout_edges = await scan_shelf_scout_line(
					tool,
					x_mm=scout_x_mm,
					y_positions=scout_y_positions,
					settle_ms=settle_ms,
					samples_per_point=samples_per_point,
					progress_label=f"shelf_scout_{scout_idx}",
				)
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
				await set_y_brake_for_scouting(is_enabled=True)

	filtered_shelf_edges: list[ShelfSurfaceEdge] = []

	for edge in sorted(shelf_edges, key=lambda current: -current.y_mm):
		if edge.y_mm > bounds.y_max - SHELF_EDGE_TOP_MARGIN_MM:
			continue

		if len(filtered_shelf_edges) == 0:
			filtered_shelf_edges.append(edge)
			continue

		if abs(filtered_shelf_edges[-1].y_mm - edge.y_mm) < SHELF_MIN_SPACING_MM:
			continue

		filtered_shelf_edges.append(edge)

	print_status(
		"shelf_edges="
		f"{len(filtered_shelf_edges)} "
		f"y={[round(edge.y_mm, 2) for edge in filtered_shelf_edges]}"
	)

	if shelf_scout_only is True:
		return coarse_scan, 0.0, [], filtered_shelf_edges

	for shelf_idx, shelf_edge in enumerate(filtered_shelf_edges, start=1):
		for offset_idx, offset_mm in enumerate(SHELF_HOLE_SWEEP_OFFSETS_MM, start=1):
			sweep_y_mm = shelf_edge.y_mm + offset_mm

			if sweep_y_mm < bounds.y_min or sweep_y_mm > bounds.y_max:
				continue

			print_status(
				f"phase=shelf-hole-scan shelf={shelf_idx}/{len(filtered_shelf_edges)} "
				f"offset={offset_idx}/{len(SHELF_HOLE_SWEEP_OFFSETS_MM)} y={sweep_y_mm:.2f}"
			)
			sweep_depth_values = await scan_line_positions(
				tool,
				axis="x",
				positions=hole_sweep_x_positions,
				fixed_mm=sweep_y_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_label=f"shelf_{shelf_idx}_offset_{offset_idx}",
			)
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
					"offset_count": len(SHELF_HOLE_SWEEP_OFFSETS_MM),
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
	y_margin_mm = max(
		FINE_SCAN_MARGIN_Y_MM + 10.0,
		(scan_config.expected_hole_height_mm / 2.0) + 12.0,
	)

	return ScanBounds(
		x_min=max(overall_bounds.x_min, region.x_min - FINE_SCAN_MARGIN_X_MM),
		x_max=min(overall_bounds.x_max, region.x_max + FINE_SCAN_MARGIN_X_MM),
		y_min=max(overall_bounds.y_min, region.y_min - y_margin_mm),
		y_max=min(overall_bounds.y_max, region.y_max + y_margin_mm),
	)


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


async def sample_scan_point(
	tool: Any,
	*,
	x_mm: float,
	y_mm: float,
	settle_ms: int,
	samples_per_point: int,
) -> float | None:
	await tool._my.move_to_position_no_wait(y_mm)
	await tool._mx.move_to_position_no_wait(x_mm)
	await tool.gantry_wait()
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
		depth_mm = await didSample(position_idx)

		if is_hole_interior_depth(
			depth_mm,
			wall_depth_mm=wall_depth_mm,
		) is True:
			return position_idx

	for position_idx in range(start_idx + 1, len(positions)):
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
) -> list[float | None]:
	if len(positions) == 0:
		return []

	first_position_mm = positions[0]

	if axis == "x":
		await tool._my.move_to_position_no_wait(fixed_mm)
		await tool._mx.move_to_position_no_wait(first_position_mm)
	else:
		await tool._mx.move_to_position_no_wait(fixed_mm)
		await tool._my.move_to_position_no_wait(first_position_mm)

	await tool.gantry_wait()

	depth_values: list[float | None] = []

	for position_idx, position_mm in enumerate(positions):
		if position_idx > 0:
			if axis == "x":
				await tool._mx.move_to_position_no_wait(position_mm)
			else:
				await tool._my.move_to_position_no_wait(position_mm)

			await tool.gantry_wait()

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
) -> tuple[list[float | None], list[ShelfSurfaceEdge]]:
	if len(y_positions) == 0:
		return [], []

	await tool._mx.move_to_position_no_wait(x_mm)
	await tool._my.move_to_position_no_wait(y_positions[0])
	await tool.gantry_wait()

	depth_values: list[float | None] = [None for _ in y_positions]
	detected_edges: list[ShelfSurfaceEdge] = []
	position_idx = 0
	sampled_count = 0
	run_start_idx: Optional[int] = None
	run_end_idx: Optional[int] = None

	while position_idx < len(y_positions):
		y_mm = y_positions[position_idx]

		if position_idx > 0:
			await tool._my.move_to_position_no_wait(y_mm)
			await tool.gantry_wait()

		depth_mm = await sample_laser_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		depth_values[position_idx] = depth_mm
		sampled_count += 1

		if sampled_count % progress_every == 0 or position_idx + 1 == len(y_positions):
			print_status(
				f"{progress_label}={sampled_count}/{len(y_positions)} "
				f"x={x_mm:.2f} "
				f"y={y_mm:.2f} "
				f"depth={depth_to_label(depth_mm)}"
			)

		if is_positive_depth(depth_mm) is True:
			if run_start_idx is None:
				run_start_idx = position_idx

			run_end_idx = position_idx
			position_idx += 1
			continue

		if run_start_idx is None or run_end_idx is None:
			position_idx += 1
			continue

		run_length = run_end_idx - run_start_idx + 1
		band_height_mm = abs(y_positions[run_end_idx] - y_positions[run_start_idx]) + SHELF_SCOUT_STEP_MM
		is_bounded_band = run_start_idx > 0

		if (
			run_length >= SHELF_EDGE_CONFIRMATION_COUNT and
			is_bounded_band is True and
			band_height_mm >= SHELF_BRIM_MIN_HEIGHT_MM and
			band_height_mm <= SHELF_BRIM_MAX_HEIGHT_MM
		):
			center_y_mm = float((y_positions[run_start_idx] + y_positions[run_end_idx]) / 2.0)
			detected_edges.append(
				ShelfSurfaceEdge(
					x_mm=x_mm,
					y_mm=center_y_mm,
				)
			)

			skip_target_y_mm = y_positions[run_end_idx] - SHELF_SCOUT_POST_EDGE_SKIP_MM

			while position_idx < len(y_positions) and y_positions[position_idx] > skip_target_y_mm:
				position_idx += 1

			run_start_idx = None
			run_end_idx = None
			continue

		run_start_idx = None
		run_end_idx = None
		position_idx += 1

	if run_start_idx is not None and run_end_idx is not None:
		run_length = run_end_idx - run_start_idx + 1
		band_height_mm = abs(y_positions[run_end_idx] - y_positions[run_start_idx]) + SHELF_SCOUT_STEP_MM
		is_bounded_band = run_start_idx > 0 and run_end_idx < len(y_positions) - 1

		if (
			run_length >= SHELF_EDGE_CONFIRMATION_COUNT and
			is_bounded_band is True and
			band_height_mm >= SHELF_BRIM_MIN_HEIGHT_MM and
			band_height_mm <= SHELF_BRIM_MAX_HEIGHT_MM
		):
			center_y_mm = float((y_positions[run_start_idx] + y_positions[run_end_idx]) / 2.0)
			detected_edges.append(
				ShelfSurfaceEdge(
					x_mm=x_mm,
					y_mm=center_y_mm,
				)
			)

	return depth_values, detected_edges


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

	center_depth_mm = await didSample(center_idx)

	if is_hole_interior_depth(
		center_depth_mm,
		wall_depth_mm=wall_depth_mm,
	) is False:
		return None

	left_gap_idx = center_idx
	right_gap_idx = center_idx

	for position_idx in range(center_idx - 1, -1, -1):
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

	return ScanDetectedCell(
		shelf_idx=0,
		cell_idx=0,
		center_x_mm=center_x_mm,
		center_y_mm=center_y_mm,
		tool_x_mm=center_x_mm - scan_config.laser_offset_x_mm,
		tool_y_mm=center_y_mm - scan_config.laser_offset_y_mm,
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


def find_components(mask: np.ndarray) -> list[list[tuple[int, int]]]:
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
			queue: list[tuple[int, int]] = [(row_idx, col_idx)]
			visited[row_idx, col_idx] = True

			while len(queue) > 0:
				current_row, current_col = queue.pop(0)
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


def count_contiguous_wall_in_row(
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


def component_dimensions_mm(
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
	shelf_threshold_mm: float,
) -> list[ScanDetectedShelf]:
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


def detect_gap_cells(
	*,
	side: StorageModuleSide,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	scan_config: Any,
	scan_result: GridScanResult,
) -> tuple[float, list[ScanDetectedCell], list[ScanDetectedShelf]]:
	depth_array = np.array(
		[
			[np.nan if depth_mm is None else float(depth_mm) for depth_mm in row]
			for row in scan_result.depth_rows
		],
		dtype=np.float32,
	)

	valid_mask = np.isfinite(depth_array)
	positive_mask = valid_mask & (depth_array > GAP_DEPTH_THRESHOLD_MM)
	positive_values = depth_array[positive_mask]

	if positive_values.size == 0:
		return 0.0, [], []

	wall_depth_mm = estimate_wall_depth_mm(scan_result.depth_rows)
	filled_hole_mask = positive_mask & (
		depth_array >= wall_depth_mm + FILLED_HOLE_MIN_DEPTH_DELTA_MM
	)
	wall_mask = positive_mask & (
		np.abs(depth_array - wall_depth_mm) <= scan_config.wall_depth_tolerance_mm
	) & (~filled_hole_mask)
	gap_mask = valid_mask & (
		(depth_array <= GAP_DEPTH_THRESHOLD_MM) |
		filled_hole_mask
	)
	components = find_components(gap_mask)
	min_vertical_rows = max(1, int(np.ceil(scan_config.min_vertical_wall_coverage_mm / step_mm)))
	min_horizontal_cols = max(1, int(np.ceil(scan_config.min_horizontal_wall_coverage_mm / step_mm)))
	grid_height, grid_width = depth_array.shape
	detected_cells: list[ScanDetectedCell] = []

	for component in components:
		min_row, max_row, min_col, max_col, width_mm, height_mm = component_dimensions_mm(
			component,
			x_positions=scan_result.x_positions,
			y_positions=scan_result.y_positions,
			step_mm=step_mm,
		)

		if min_row == 0 or min_col == 0:
			continue
		if max_row == grid_height - 1 or max_col == grid_width - 1:
			continue
		if dimensions_match(
			width_mm,
			expected_mm=scan_config.expected_hole_width_mm,
			tolerance_mm=scan_config.hole_width_tolerance_mm + step_mm,
		) is False:
			continue
		if dimensions_match(
			height_mm,
			expected_mm=scan_config.expected_hole_height_mm,
			tolerance_mm=scan_config.hole_height_tolerance_mm + step_mm,
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
		top_wall_hits = count_contiguous_wall_in_row(
			wall_mask,
			row_idx=min_row - 1,
			center_col=center_col,
		)
		bottom_wall_hits = count_contiguous_wall_in_row(
			wall_mask,
			row_idx=max_row + 1,
			center_col=center_col,
		)

		if top_wall_hits < min_horizontal_cols:
			continue
		if bottom_wall_hits < min_horizontal_cols:
			continue

		min_x_mm = min(scan_result.x_positions[min_col], scan_result.x_positions[max_col])
		max_x_mm = max(scan_result.x_positions[min_col], scan_result.x_positions[max_col])
		min_y_mm = min(scan_result.y_positions[min_row], scan_result.y_positions[max_row])
		max_y_mm = max(scan_result.y_positions[min_row], scan_result.y_positions[max_row])
		left_edge_mm = min_x_mm - (step_mm / 2.0)
		right_edge_mm = max_x_mm + (step_mm / 2.0)
		top_edge_mm = min_y_mm - (step_mm / 2.0)
		bottom_edge_mm = max_y_mm + (step_mm / 2.0)
		center_x_mm = float((left_edge_mm + right_edge_mm) / 2.0)
		center_y_mm = float((top_edge_mm + bottom_edge_mm) / 2.0)

		detected_cells.append(
			ScanDetectedCell(
				shelf_idx=0,
				cell_idx=0,
				center_x_mm=center_x_mm,
				center_y_mm=center_y_mm,
				tool_x_mm=center_x_mm - scan_config.laser_offset_x_mm,
				tool_y_mm=center_y_mm - scan_config.laser_offset_y_mm,
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
	shelves = build_detected_shelves(
		detected_cells,
		shelf_threshold_mm=get_shelf_grouping_threshold_mm(
			step_mm=step_mm,
			scan_config=scan_config,
		),
	)

	return wall_depth_mm, detected_cells, shelves


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
) -> tuple[GridScanResult, list[ScanDetectedCell]]:
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
		row_idx = find_nearest_position_index(y_positions, target_y_mm)

		if row_idx in horizontal_probe_indices:
			continue

		horizontal_probe_indices.append(row_idx)

	horizontal_segment: Optional[LineGapSegment] = None

	for probe_idx, row_idx in enumerate(horizontal_probe_indices, start=1):
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
	recorder: Optional[StagedScanRecorder] = None,
) -> ScanAnalysisResult:
	tool = robot._tool
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

	global CURRENT_SCAN_LOG_PATH
	previous_scan_log_path = CURRENT_SCAN_LOG_PATH
	CURRENT_SCAN_LOG_PATH = recorder.log_path
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
		if scan_started_at is None:
			scan_started_at = get_current_timestamp()
			persist_scan_started_at(
				recorder=recorder,
				scan_started_at=scan_started_at,
			)

		await enter_scan_mode(tool, side, scan_config)

		if len(candidates) == 0:
			print_status("phase=coarse-scan")

			coarse_scan, wall_depth_mm, candidates, shelf_edges = await capture_shelf_candidates(
				tool,
				bounds=bounds,
				settle_ms=coarse_settle_ms,
				samples_per_point=coarse_samples_per_point,
				scan_config=scan_config,
				recorder=recorder,
				shelf_scout_only=shelf_scout_only,
				known_shelf_edge_y=known_shelf_edge_y,
			)

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

		for candidate_idx, candidate in enumerate(candidates, start=1):
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
				bounds=fine_bounds,
				step_mm=fine_step_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				wall_depth_mm=wall_depth_mm,
				scan_config=scan_config,
				progress_prefix=f"fine_{candidate_idx}",
				on_update=didUpdateFineScan,
			)
			print_status(
				f"fine_detected_cells={len(detected_cells)} "
				f"candidate={candidate_idx}/{len(candidates)}"
			)
			fine_scan_results.append((fine_bounds, fine_scan, detected_cells))
			all_detected_cells.extend(detected_cells)
			fine_entries[candidate_idx - 1] = {
				"candidate_index": candidate_idx - 1,
				"status": "completed",
				"bounds": fine_bounds.model_dump(),
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
		await tool.retract()
		CURRENT_SCAN_LOG_PATH = previous_scan_log_path

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

	if anchor.endswith("right"):
		return max(target_shelf.cells, key=lambda cell: float(cell.calibration_point.x))

	return min(target_shelf.cells, key=lambda cell: float(cell.calibration_point.x))


def apply_xy_reference_offsets(
	*,
	scanned_shelves: list[Any],
	scan_config: Any,
	side: StorageModuleSide,
) -> None:
	xy_reference = get_scan_xy_reference(scan_config=scan_config, side=side)

	if xy_reference is None:
		print_status("xy_offset_inference=skipped reason=reference_unconfigured")
		return

	anchor_cell = get_scanned_anchor_cell(
		scanned_shelves=scanned_shelves,
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

	if z_delta is None:
		print_status("z_offset_inference=skipped reason=z_delta_unresolved")
		return

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

	if z_delta["source"] == "configured_delta":
		print_status(
			f"z_offset_inference=applied source=configured_delta "
			f"delta_mm={z_delta_mm:.3f} updated_cells={updated_cell_count}"
		)
		return

	print_status(
		f"z_offset_inference=applied reference={reference_key[0]}:{reference_key[1]} "
		f"reference_z_offset_mm={reference_key[2]:.3f} "
		f"reference_wall_depth_mm={reference_key[3]:.3f} "
		f"delta_mm={z_delta_mm:.3f} updated_cells={updated_cell_count}"
	)


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
	capture = create_scan_capture(
		side=side,
		bounds=bounds,
		step_mm=step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		laser_offset_x_mm=scan_config.laser_offset_x_mm,
		laser_offset_y_mm=scan_config.laser_offset_y_mm,
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


async def run_scan(args: argparse.Namespace) -> int:
	print_status("phase=config")
	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	recorder: Optional[StagedScanRecorder] = None

	if args.continue_scan is True:
		(
			recorder,
			side,
			bounds,
			step_mm,
			settle_ms,
			samples_per_point,
			coarse_settle_ms,
			coarse_samples_per_point,
		) = load_continue_scan_context(args.scans_dir)
		run_parameters_document = load_json_file(recorder.run_parameters_path)
		run_parameters = run_parameters_document["run_parameters"]
		shelf_scout_only = bool(run_parameters.get("cli", {}).get("shelf_scout_only", False))
		known_shelf_edge_y = run_parameters.get("cli", {}).get("known_shelf_edge_y")
		print_status(f"resume_run_id={recorder.run_id}")
	else:
		if args.module is None:
			raise ValueError("--module is required unless --continue is used")
		if args.x is None:
			raise ValueError("--x is required unless --continue is used")
		if args.y is None:
			raise ValueError("--y is required unless --continue is used")
		if args.step is None:
			raise ValueError("--step is required unless --continue is used")

		side = normalize_module(args.module)
		step_mm = float(args.step)
		settle_ms = (
			args.settle_ms
			if args.settle_ms is not None
			else get_fine_settle_ms(scan_config.settle_ms)
		)
		samples_per_point = (
			args.samples_per_point
			if args.samples_per_point is not None
			else get_fine_samples_per_point(scan_config.samples_per_point)
		)

		coarse_settle_ms = get_coarse_settle_ms(settle_ms)
		coarse_samples_per_point = get_coarse_samples_per_point(samples_per_point)

		bounds = derive_bounds(args)
		run_parameters = build_run_parameters(
			args=args,
			side=side,
			bounds=bounds,
			step_mm=step_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			coarse_settle_ms=coarse_settle_ms,
			coarse_samples_per_point=coarse_samples_per_point,
			scan_config=scan_config,
		)
		shelf_scout_only = bool(args.shelf_scout_only)
		known_shelf_edge_y = (
			[float(value) for value in args.known_shelf_edge_y]
			if args.known_shelf_edge_y is not None
			else None
		)

	if step_mm <= 0:
		raise ValueError("--step must be greater than zero")

	if settle_ms < 0:
		raise ValueError("--settle-ms must be zero or greater")

	if samples_per_point <= 0:
		raise ValueError("--samples-per-point must be greater than zero")

	if coarse_settle_ms < 0:
		raise ValueError("coarse settle must be zero or greater")

	if coarse_samples_per_point <= 0:
		raise ValueError("coarse samples per point must be greater than zero")

	grid_path = Path(resolve_grid_path(config))
	grid = load_grid(grid_path)

	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)
	total_points = len(x_positions) * len(y_positions)
	print_status(
		f"scan=module:{side.value} "
		f"bounds:x=({bounds.x_min:.2f},{bounds.x_max:.2f}) "
		f"y=({bounds.y_min:.2f},{bounds.y_max:.2f}) "
		f"fine_step_mm:{step_mm:.2f} "
		f"coarse_step_mm:{COARSE_SCAN_STEP_MM:.2f} "
		f"fine_settle_ms:{settle_ms} "
		f"coarse_settle_ms:{coarse_settle_ms} "
		f"fine_samples_per_point:{samples_per_point} "
		f"coarse_samples_per_point:{coarse_samples_per_point} "
		f"points:{total_points}"
	)

	if known_shelf_edge_y is not None:
		print_status(f"known_shelf_edges_y={[round(float(value), 2) for value in known_shelf_edge_y]}")

	if args.npy is not None:
		if args.continue_scan is True:
			raise ValueError("--npy cannot be used with --continue")

		print_status("phase=load-npy")
		depth_rows = load_depth_rows_from_npy(
			args.npy,
			expected_row_count=len(y_positions),
			expected_column_count=len(x_positions),
		)
		print_status(f"npy={args.npy}")
		print_status("phase=analysis")
		analysis = analyze_depth_rows(
			side=side,
			bounds=bounds,
			step_mm=step_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			scan_config=scan_config,
			scans_dir=args.scans_dir,
			depth_rows=depth_rows,
		)
	else:
		print_status("phase=robot-init")
		with tempfile.NamedTemporaryFile(
			mode="w",
			suffix=".json",
			prefix="laser-script-config-",
			dir=str(Path(args.config).resolve().parent),
			delete=False,
		) as temp_config_file:
			temp_config_path = Path(temp_config_file.name)
			temp_config_data = build_standalone_config(config)
			json.dump(temp_config_data, temp_config_file, indent="\t")

		original_common_config_path = common_module.CONFIG_PATH
		original_robot_config_path = robot_module.CONFIG_PATH

		common_module.CONFIG_PATH = temp_config_path
		robot_module.CONFIG_PATH = temp_config_path

		try:
			robot = Robot()
			print_status("phase=robot-configure")
			await robot.configure()

			try:
				print_status("phase=scan-start")
				analysis = await run_staged_scan(
					robot=robot,
					side=side,
					bounds=bounds,
					fine_step_mm=step_mm,
					settle_ms=settle_ms,
					samples_per_point=samples_per_point,
					coarse_settle_ms=coarse_settle_ms,
					coarse_samples_per_point=coarse_samples_per_point,
					scan_config=scan_config,
					scans_dir=args.scans_dir,
					run_parameters=run_parameters,
					shelf_scout_only=shelf_scout_only,
					known_shelf_edge_y=known_shelf_edge_y,
					recorder=recorder,
				)
			finally:
				print_status("phase=robot-shutdown")
				await robot.shutdown()
		finally:
			common_module.CONFIG_PATH = original_common_config_path
			robot_module.CONFIG_PATH = original_robot_config_path
			temp_config_path.unlink(missing_ok=True)

	run_parameters_path = persist_run_parameters(
		run_dir=Path(analysis.raw_scan_path).resolve().parent,
		run_id=analysis.run_id,
		run_parameters=run_parameters,
	)

	if args.apply is True:
		print_status("phase=apply")
		apply_analysis(
			analysis=analysis,
			grid=grid,
			grid_path=grid_path,
			scan_config=scan_config,
			side=side,
		)

	print_analysis(analysis)
	print(f"run_parameters_path: {run_parameters_path}")
	return 0


def main() -> int:
	args = parse_args()

	try:
		return asyncio.run(run_scan(args))
	except KeyboardInterrupt:
		return 130
	except Exception as exc:
		traceback.print_exc()
		print(str(exc), file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
