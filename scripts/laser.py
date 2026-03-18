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
COARSE_SCAN_STEP_MM = 8.0
COARSE_EMPTY_ROW_SKIP_MM = 50.0
COARSE_EMPTY_ROW_STREAK = 2
GAP_DEPTH_THRESHOLD_MM = 0.01
FINE_SCAN_MARGIN_X_MM = 10.0
FINE_SCAN_MARGIN_Y_MM = 10.0
COARSE_WALL_SEARCH_STEPS = 2
CURRENT_SCAN_LOG_PATH: Optional[Path] = None


@dataclass
class GridScanResult:
	x_positions: list[float]
	y_positions: list[float]
	depth_rows: list[list[float | None]]


@dataclass
class RowGapSegment:
	x_min: float
	x_max: float
	y_mm: float


@dataclass
class GapCandidateRegion:
	x_min: float
	x_max: float
	y_min: float
	y_max: float
	row_count: int = 1


@dataclass
class StagedScanRecorder:
	run_id: str
	run_dir: Path
	coarse_depth_path: Path
	metadata_path: Path
	run_parameters_path: Path
	progress_path: Path
	log_path: Path


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run a laser tray scan and print detected calibration points.")
	parser.add_argument("--module", choices=["left", "right"])
	parser.add_argument("--x", nargs=2, metavar=("MIN", "MAX"), type=float)
	parser.add_argument("--y", nargs=2, metavar=("MIN", "MAX"), type=float)
	parser.add_argument("--y-offset", type=float, default=0.0)
	parser.add_argument("--step", type=float)
	parser.add_argument("--settle-ms", type=int)
	parser.add_argument("--samples-per-point", type=int)
	parser.add_argument("--npy", type=Path)
	parser.add_argument("--scans-dir", default="/tmp")
	parser.add_argument("--config", default=str(CONFIG_PATH))
	parser.add_argument("--apply", action="store_true")
	parser.add_argument("--continue", dest="continue_scan", action="store_true")
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
) -> dict[str, Any]:
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
			"npy": str(args.npy) if args.npy is not None else None,
			"scans_dir": args.scans_dir,
			"config": args.config,
			"apply": args.apply,
		},
		"resolved": {
			"bounds": bounds.model_dump(),
			"fine_step_mm": step_mm,
			"coarse_step_mm": COARSE_SCAN_STEP_MM,
			"settle_ms": settle_ms,
			"samples_per_point": samples_per_point,
			"laser_scan_stage_extension_mm": LASER_SCAN_STAGE_EXTENSION_MM,
			"laser_scan_z_offset_mm": LASER_SCAN_Z_OFFSET_MM,
			"laser_scan_y_offset_mm": float(args.y_offset),
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
				"created_at": datetime.now(timezone.utc).isoformat(),
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
) -> tuple[StagedScanRecorder, StorageModuleSide, ScanBounds, float, int, int]:
	recorder = find_latest_incomplete_run(scans_dir)
	run_parameters_document = load_json_file(recorder.run_parameters_path)
	run_parameters = run_parameters_document["run_parameters"]
	resolved_parameters = run_parameters["resolved"]
	side = StorageModuleSide(run_parameters["module"])
	bounds = ScanBounds.model_validate(resolved_parameters["bounds"])
	step_mm = float(resolved_parameters["fine_step_mm"])
	settle_ms = int(resolved_parameters["settle_ms"])
	samples_per_point = int(resolved_parameters["samples_per_point"])

	return recorder, side, bounds, step_mm, settle_ms, samples_per_point


def load_candidates_from_metadata(metadata: dict[str, Any]) -> list[GapCandidateRegion]:
	return [
		GapCandidateRegion(
			x_min=float(entry["x_min"]),
			x_max=float(entry["x_max"]),
			y_min=float(entry["y_min"]),
			y_max=float(entry["y_max"]),
			row_count=int(entry.get("row_count", 1)),
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
				"updated_at": datetime.now(timezone.utc).isoformat(),
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
	wall_depth_mm: float,
	coarse_scan: GridScanResult,
	candidates: list[GapCandidateRegion],
	fine_entries: list[dict[str, Any]],
	status: str,
) -> None:
	recorder.metadata_path.write_text(
		json.dumps(
			{
				"run_id": recorder.run_id,
				"created_at": datetime.now(timezone.utc).isoformat(),
				"status": status,
				"side": side.value,
				"bounds": bounds.model_dump(),
				"fine_step_mm": fine_step_mm,
				"coarse_step_mm": COARSE_SCAN_STEP_MM,
				"settle_ms": settle_ms,
				"samples_per_point": samples_per_point,
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
					}
					for candidate in candidates
				],
				"fine_scans": fine_entries,
			},
			indent="\t",
		),
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


async def get_laser_scan_z_extension_mm(tool: Any) -> float:
	z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")

	if z_extension_place is None:
		raise ValueError("Z_EXTENSION_PLACE parameter is not configured")

	return float(z_extension_place) + LASER_SCAN_Z_OFFSET_MM


async def ensure_scan_tool_ready(tool: Any) -> None:
	if tool._laser_enabled is False or tool._laser is None:
		raise ValueError("Laser is not enabled")

	if tool._mx is None or tool._my is None:
		raise ValueError("Gantry motors (x_axis, y_axis) are required for laser scan")

	if tool._ms is None or tool._mg is None:
		raise ValueError("Tool motors (stage, z_axis) are required for laser scan")


async def enter_scan_mode(tool: Any, side: StorageModuleSide) -> None:
	await ensure_scan_tool_ready(tool)

	side_number = 0
	target_position = "front"

	if side == StorageModuleSide.RIGHT:
		side_number = 1
		target_position = "back"

	stage_extension_mm = LASER_SCAN_STAGE_EXTENSION_MM
	z_extension_mm = await get_laser_scan_z_extension_mm(tool)
	z_move_rpm = await tool.get_param("Z_MOVE_RPM")
	z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

	if z_move_rpm is None or z_move_accel is None:
		raise ValueError("Z_MOVE_RPM and Z_MOVE_ACCEL parameters are required for laser scan")

	scan_x_mm, scan_y_mm = await get_scan_position(tool)

	print_status(
		f"scan_setup=module:{side.value} "
		f"scan_position:({scan_x_mm:.2f},{scan_y_mm:.2f}) "
		f"stage_extension_mm:{stage_extension_mm:.2f} "
		f"z_extension_mm:{z_extension_mm:.2f}"
	)

	await tool.retract()
	await tool.move(AxisCoordinate(x=scan_x_mm, y=scan_y_mm, s=side_number))
	await tool.rotate_to_position(target_position)
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


async def scan_row(
	tool: Any,
	*,
	x_positions: list[float],
	y_position: float,
	row_idx: int,
	settle_ms: int,
	samples_per_point: int,
	progress_label: str,
	progress_every: int = 25,
) -> list[float | None]:
	row_values: list[float | None] = [None] * len(x_positions)
	column_indices = list(range(len(x_positions)))

	if row_idx % 2 == 1:
		column_indices = list(range(len(x_positions) - 1, -1, -1))

	row_start_idx = column_indices[0]
	row_start_x = x_positions[row_start_idx]
	await tool._my.move_to_position_no_wait(y_position)
	await tool._mx.move_to_position_no_wait(row_start_x)
	await tool.gantry_wait()

	for position_idx, column_idx in enumerate(column_indices):
		x_position = x_positions[column_idx]

		if position_idx > 0:
			await tool._mx.move_to_position_no_wait(x_position)
			await tool.gantry_wait()

		row_values[column_idx] = await sample_laser_depth(
			tool,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)

		if (position_idx + 1) % progress_every != 0 and position_idx + 1 != len(column_indices):
			continue

		print_status(
			f"{progress_label}={position_idx + 1}/{len(column_indices)} "
			f"x={x_position:.2f} "
			f"y={y_position:.2f} "
			f"depth={depth_to_label(row_values[column_idx])}"
		)

	return row_values


async def scan_grid(
	tool: Any,
	*,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	progress_prefix: str,
	existing_scan: Optional[GridScanResult] = None,
	on_row_complete: Optional[Callable[[GridScanResult, int, float], None]] = None,
) -> GridScanResult:
	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)
	depth_rows: list[list[float | None]] = []
	start_row_idx = 0

	if existing_scan is not None:
		if existing_scan.x_positions != x_positions:
			raise ValueError("Saved fine scan x positions do not match current bounds")

		if y_positions[:len(existing_scan.y_positions)] != existing_scan.y_positions:
			raise ValueError("Saved fine scan y positions do not match current bounds")

		depth_rows = [list(row) for row in existing_scan.depth_rows]
		start_row_idx = len(existing_scan.y_positions)

	for row_idx in range(start_row_idx, len(y_positions)):
		y_position = y_positions[row_idx]
		row_values = await scan_row(
			tool,
			x_positions=x_positions,
			y_position=y_position,
			row_idx=row_idx,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			progress_label=f"{progress_prefix}_row_{row_idx + 1}",
		)
		depth_rows.append(row_values)

		if on_row_complete is not None:
			on_row_complete(
				GridScanResult(
					x_positions=x_positions,
					y_positions=y_positions[: row_idx + 1],
					depth_rows=[list(row) for row in depth_rows],
				),
				row_idx,
				y_position,
			)

	return GridScanResult(
		x_positions=x_positions,
		y_positions=y_positions,
		depth_rows=depth_rows,
	)


async def capture_coarse_scan(
	tool: Any,
	*,
	bounds: ScanBounds,
	settle_ms: int,
	samples_per_point: int,
	existing_scan: Optional[GridScanResult] = None,
	on_row_complete: Optional[Callable[[GridScanResult, int, float], None]] = None,
) -> GridScanResult:
	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, COARSE_SCAN_STEP_MM)
	y_positions: list[float] = []
	depth_rows: list[list[float | None]] = []
	empty_row_streak = 0
	current_y = bounds.y_min
	row_idx = 0

	if existing_scan is not None:
		if existing_scan.x_positions != x_positions:
			raise ValueError("Saved coarse scan x positions do not match current bounds")

		y_positions = list(existing_scan.y_positions)
		depth_rows = [list(row) for row in existing_scan.depth_rows]
		row_idx = len(y_positions)

		for row_values in depth_rows:
			row_has_positive_depth = any(is_positive_depth(depth_mm) is True for depth_mm in row_values)

			if row_has_positive_depth is True:
				empty_row_streak = 0
				continue

			empty_row_streak += 1

		if len(y_positions) > 0:
			last_y_position = y_positions[-1]
			next_step_y_mm = COARSE_SCAN_STEP_MM

			if empty_row_streak >= COARSE_EMPTY_ROW_STREAK:
				next_step_y_mm = COARSE_EMPTY_ROW_SKIP_MM

			current_y = last_y_position + next_step_y_mm

	while True:
		y_position = round(min(current_y, bounds.y_max), 4)

		if len(y_positions) > 0 and y_position == y_positions[-1]:
			break

		row_values = await scan_row(
			tool,
			x_positions=x_positions,
			y_position=y_position,
			row_idx=row_idx,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			progress_label=f"coarse_row_{row_idx + 1}",
		)

		y_positions.append(y_position)
		depth_rows.append(row_values)

		if on_row_complete is not None:
			on_row_complete(
				GridScanResult(
					x_positions=x_positions,
					y_positions=list(y_positions),
					depth_rows=[list(row) for row in depth_rows],
				),
				row_idx,
				y_position,
			)

		row_has_positive_depth = any(is_positive_depth(depth_mm) is True for depth_mm in row_values)

		if row_has_positive_depth is True:
			empty_row_streak = 0
		else:
			empty_row_streak += 1

		if y_position >= bounds.y_max:
			break

		step_y_mm = COARSE_SCAN_STEP_MM

		if empty_row_streak >= COARSE_EMPTY_ROW_STREAK:
			step_y_mm = COARSE_EMPTY_ROW_SKIP_MM

		current_y = y_position + step_y_mm
		row_idx += 1

	return GridScanResult(
		x_positions=x_positions,
		y_positions=y_positions,
		depth_rows=depth_rows,
	)


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


def get_coarse_candidate_width_limits(
	*,
	expected_hole_width_mm: float,
	hole_width_tolerance_mm: float,
) -> tuple[float, float]:
	min_candidate_width_mm = max(
		COARSE_SCAN_STEP_MM * 2.0,
		expected_hole_width_mm - max(hole_width_tolerance_mm / 2.0, COARSE_SCAN_STEP_MM / 2.0),
	)
	max_candidate_width_mm = expected_hole_width_mm + hole_width_tolerance_mm + (COARSE_SCAN_STEP_MM * 2.0)

	return min_candidate_width_mm, max_candidate_width_mm


def get_coarse_candidate_height_limits(
	*,
	expected_hole_height_mm: float,
	hole_height_tolerance_mm: float,
) -> tuple[float, float]:
	min_candidate_height_mm = max(
		COARSE_SCAN_STEP_MM * 2.0,
		expected_hole_height_mm - max(hole_height_tolerance_mm / 2.0, COARSE_SCAN_STEP_MM / 2.0),
	)
	max_candidate_height_mm = expected_hole_height_mm + hole_height_tolerance_mm + COARSE_SCAN_STEP_MM

	return min_candidate_height_mm, max_candidate_height_mm


def find_row_gap_segments(
	*,
	row_values: list[float | None],
	x_positions: list[float],
	y_mm: float,
	wall_depth_mm: float,
	wall_tolerance_mm: float,
	expected_hole_width_mm: float,
	hole_width_tolerance_mm: float,
) -> list[RowGapSegment]:
	segments: list[RowGapSegment] = []
	start_idx: Optional[int] = None

	min_candidate_width_mm, max_candidate_width_mm = get_coarse_candidate_width_limits(
		expected_hole_width_mm=expected_hole_width_mm,
		hole_width_tolerance_mm=hole_width_tolerance_mm,
	)

	for column_idx, depth_mm in enumerate(row_values):
		if is_gap_depth(depth_mm) is True:
			if start_idx is None:
				start_idx = column_idx
			continue

		if start_idx is None:
			continue

		end_idx = column_idx - 1
		left_has_wall = has_wall_nearby(
			row_values,
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=wall_tolerance_mm,
			start_idx=start_idx - 1,
			direction=-1,
		)
		right_has_wall = has_wall_nearby(
			row_values,
			wall_depth_mm=wall_depth_mm,
			tolerance_mm=wall_tolerance_mm,
			start_idx=end_idx + 1,
			direction=1,
		)
		width_mm = abs(x_positions[end_idx] - x_positions[start_idx]) + COARSE_SCAN_STEP_MM

		if (
			left_has_wall is True and
			right_has_wall is True and
			width_mm >= min_candidate_width_mm and
			width_mm <= max_candidate_width_mm
		):
			segments.append(
				RowGapSegment(
					x_min=min(x_positions[start_idx], x_positions[end_idx]),
					x_max=max(x_positions[start_idx], x_positions[end_idx]),
					y_mm=y_mm,
				)
			)

		start_idx = None

	return segments


def merge_candidate_segments(
	segments: list[RowGapSegment],
	*,
	expected_hole_width_mm: float,
	expected_hole_height_mm: float,
	hole_width_tolerance_mm: float,
	hole_height_tolerance_mm: float,
) -> list[GapCandidateRegion]:
	regions: list[GapCandidateRegion] = []

	for segment in sorted(segments, key=lambda current: (current.y_mm, current.x_min)):
		matched_region: Optional[GapCandidateRegion] = None

		for region in regions:
			if segment.y_mm > region.y_max + (COARSE_SCAN_STEP_MM * 1.5):
				continue
			if segment.x_min > region.x_max + COARSE_SCAN_STEP_MM:
				continue
			if segment.x_max < region.x_min - COARSE_SCAN_STEP_MM:
				continue

			matched_region = region
			break

		if matched_region is None:
			regions.append(
				GapCandidateRegion(
					x_min=segment.x_min,
					x_max=segment.x_max,
					y_min=segment.y_mm,
					y_max=segment.y_mm,
				)
			)
			continue

		matched_region.x_min = min(matched_region.x_min, segment.x_min)
		matched_region.x_max = max(matched_region.x_max, segment.x_max)
		matched_region.y_min = min(matched_region.y_min, segment.y_mm)
		matched_region.y_max = max(matched_region.y_max, segment.y_mm)
		matched_region.row_count += 1

	filtered_regions: list[GapCandidateRegion] = []

	min_width_mm, max_width_mm = get_coarse_candidate_width_limits(
		expected_hole_width_mm=expected_hole_width_mm,
		hole_width_tolerance_mm=hole_width_tolerance_mm,
	)

	min_height_mm, max_height_mm = get_coarse_candidate_height_limits(
		expected_hole_height_mm=expected_hole_height_mm,
		hole_height_tolerance_mm=hole_height_tolerance_mm,
	)

	for region in regions:
		width_mm = abs(region.x_max - region.x_min) + COARSE_SCAN_STEP_MM
		height_mm = abs(region.y_max - region.y_min) + COARSE_SCAN_STEP_MM

		if width_mm < min_width_mm:
			continue
		if width_mm > max_width_mm:
			continue
		if height_mm < min_height_mm:
			continue
		if height_mm > max_height_mm:
			continue
		if region.row_count < 2:
			continue

		filtered_regions.append(region)

	return filtered_regions


def detect_coarse_candidates(
	scan_result: GridScanResult,
	*,
	scan_config: Any,
) -> tuple[float, list[GapCandidateRegion]]:
	try:
		wall_depth_mm = estimate_wall_depth_mm(scan_result.depth_rows)
	except ValueError:
		return 0.0, []

	segments: list[RowGapSegment] = []

	for row_values, y_position in zip(scan_result.depth_rows, scan_result.y_positions, strict=True):
		row_segments = find_row_gap_segments(
			row_values=row_values,
			x_positions=scan_result.x_positions,
			y_mm=y_position,
			wall_depth_mm=wall_depth_mm,
			wall_tolerance_mm=scan_config.wall_depth_tolerance_mm,
			expected_hole_width_mm=scan_config.expected_hole_width_mm,
			hole_width_tolerance_mm=scan_config.hole_width_tolerance_mm,
		)
		segments.extend(row_segments)

	candidates = merge_candidate_segments(
		segments,
		expected_hole_width_mm=scan_config.expected_hole_width_mm,
		expected_hole_height_mm=scan_config.expected_hole_height_mm,
		hole_width_tolerance_mm=scan_config.hole_width_tolerance_mm,
		hole_height_tolerance_mm=scan_config.hole_height_tolerance_mm,
	)

	return wall_depth_mm, candidates


def build_fine_scan_bounds(
	region: GapCandidateRegion,
	*,
	overall_bounds: ScanBounds,
	scan_config: Any,
) -> ScanBounds:
	return ScanBounds(
		x_min=max(overall_bounds.x_min, region.x_min - FINE_SCAN_MARGIN_X_MM),
		x_max=min(overall_bounds.x_max, region.x_max + FINE_SCAN_MARGIN_X_MM),
		y_min=max(overall_bounds.y_min, region.y_min - FINE_SCAN_MARGIN_Y_MM),
		y_max=min(overall_bounds.y_max, region.y_max + FINE_SCAN_MARGIN_Y_MM),
	)


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
	wall_mask = positive_mask & (
		np.abs(depth_array - wall_depth_mm) <= scan_config.wall_depth_tolerance_mm
	)
	gap_mask = valid_mask & (depth_array <= GAP_DEPTH_THRESHOLD_MM)
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
		shelf_threshold_mm=max(step_mm * 2.0, 5.0),
	)

	return wall_depth_mm, detected_cells, shelves


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
	coarse_scan: GridScanResult,
	candidates: list[GapCandidateRegion],
	fine_scan_results: list[tuple[ScanBounds, GridScanResult, list[ScanDetectedCell]]],
	wall_depth_mm: float,
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
		wall_depth_mm=wall_depth_mm,
		coarse_scan=coarse_scan,
		candidates=candidates,
		fine_entries=fine_entries,
		status="completed",
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
	scan_config: Any,
	scans_dir: str,
	run_parameters: dict[str, Any],
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
	wall_depth_mm = 0.0
	metadata: dict[str, Any] = {}
	progress_document: dict[str, Any] = {}
	fine_entries: list[dict[str, Any]] = []
	fine_scan_results: list[tuple[ScanBounds, GridScanResult, list[ScanDetectedCell]]] = []
	all_detected_cells: list[ScanDetectedCell] = []
	global CURRENT_SCAN_LOG_PATH
	previous_scan_log_path = CURRENT_SCAN_LOG_PATH
	CURRENT_SCAN_LOG_PATH = recorder.log_path
	print_status(f"run_dir={recorder.run_dir}")

	if recorder.metadata_path.exists() is True:
		metadata = load_json_file(recorder.metadata_path)
		candidates = load_candidates_from_metadata(metadata)
		wall_depth_mm = float(metadata.get("wall_depth_mm", 0.0))
		fine_entries = list(metadata.get("fine_scans", []))

	if recorder.progress_path.exists() is True:
		progress_document = load_json_file(recorder.progress_path)

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
			wall_depth_mm=wall_depth_mm,
			coarse_scan=coarse_scan,
			candidates=candidates,
			fine_entries=fine_entries,
			status="starting",
		)

	try:
		await enter_scan_mode(tool, side)

		if len(candidates) == 0:
			print_status("phase=coarse-scan")

			if len(coarse_scan.y_positions) > 0:
				print_status(
					f"resume=coarse rows_completed={len(coarse_scan.y_positions)} "
					f"last_y={coarse_scan.y_positions[-1]:.2f}"
				)

			def didCompleteCoarseRow(
				partial_scan: GridScanResult,
				row_idx: int,
				y_position: float,
			) -> None:
				save_depth_rows_to_npy(
					output_path=recorder.coarse_depth_path,
					depth_rows=partial_scan.depth_rows,
				)
				persist_scan_progress(
					recorder=recorder,
					phase="coarse-scan",
					progress={
						"row_idx": row_idx,
						"row_count": len(partial_scan.y_positions),
						"y_position": y_position,
					},
				)

			coarse_scan = await capture_coarse_scan(
				tool,
				bounds=bounds,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				existing_scan=coarse_scan if len(coarse_scan.y_positions) > 0 else None,
				on_row_complete=didCompleteCoarseRow,
			)

			print_status("phase=coarse-detect")
			wall_depth_mm, candidates = detect_coarse_candidates(
				coarse_scan,
				scan_config=scan_config,
			)
			print_status(
				f"coarse_candidates={len(candidates)} "
				f"coarse_rows={len(coarse_scan.y_positions)} "
				f"wall_depth_mm={wall_depth_mm:.2f}"
			)
			persist_staged_metadata(
				recorder=recorder,
				side=side,
				bounds=bounds,
				fine_step_mm=fine_step_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				wall_depth_mm=wall_depth_mm,
				coarse_scan=coarse_scan,
				candidates=candidates,
				fine_entries=fine_entries,
				status="coarse-detect",
			)
		else:
			print_status(
				f"resume=fine candidates={len(candidates)} "
				f"completed_candidates={len(fine_scan_results)}"
			)

		progress_phase = progress_document.get("phase")
		progress_payload = progress_document.get("progress", {})
		resume_candidate_idx = int(progress_payload.get("candidate_idx", 0))

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
			existing_fine_scan: Optional[GridScanResult] = None

			if (
				progress_phase == "fine-scan" and
				resume_candidate_idx == candidate_idx and
				fine_path.exists() is True
			):
				existing_fine_scan = load_partial_grid_scan(
					npy_path=fine_path,
					bounds=fine_bounds,
					step_mm=fine_step_mm,
				)
				print_status(
					f"resume=fine candidate={candidate_idx}/{len(candidates)} "
					f"rows_completed={len(existing_fine_scan.y_positions)}"
				)

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
				"abort_reason": existing_fine_entry.get("abort_reason"),
			}

			def didCompleteFineRow(
				partial_scan: GridScanResult,
				row_idx: int,
				y_position: float,
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
						"row_idx": row_idx,
						"row_count": len(partial_scan.y_positions),
						"y_position": y_position,
						"fine_path": str(fine_path),
					},
				)
				fine_entries[candidate_idx - 1]["x_positions"] = partial_scan.x_positions
				fine_entries[candidate_idx - 1]["y_positions"] = partial_scan.y_positions

			fine_abort_reason: Optional[str] = None
			fine_scan = await scan_grid(
				tool,
				bounds=fine_bounds,
				step_mm=fine_step_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_prefix=f"fine_{candidate_idx}",
				existing_scan=existing_fine_scan,
				on_row_complete=didCompleteFineRow,
			)

			detected_cells: list[ScanDetectedCell] = []

			if fine_abort_reason is None:
				_, detected_cells, _ = detect_gap_cells(
					side=side,
					bounds=fine_bounds,
					step_mm=fine_step_mm,
					settle_ms=settle_ms,
					samples_per_point=samples_per_point,
					scan_config=scan_config,
					scan_result=fine_scan,
				)
			print_status(
				f"fine_detected_cells={len(detected_cells)} "
				f"candidate={candidate_idx}/{len(candidates)}"
			)
			fine_scan_results.append((fine_bounds, fine_scan, detected_cells))
			all_detected_cells.extend(detected_cells)
			fine_entries[candidate_idx - 1] = {
				"candidate_index": candidate_idx - 1,
				"status": "aborted" if fine_abort_reason is not None else "completed",
				"bounds": fine_bounds.model_dump(),
				"raw_scan_path": str(fine_path),
				"x_positions": fine_scan.x_positions,
				"y_positions": fine_scan.y_positions,
				"detected_cells": [cell.model_dump() for cell in detected_cells],
				"abort_reason": fine_abort_reason,
			}
			persist_staged_metadata(
				recorder=recorder,
				side=side,
				bounds=bounds,
				fine_step_mm=fine_step_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				wall_depth_mm=wall_depth_mm,
				coarse_scan=coarse_scan,
				candidates=candidates,
				fine_entries=fine_entries,
				status="fine-scan",
			)
	finally:
		await tool.retract()
		CURRENT_SCAN_LOG_PATH = previous_scan_log_path

	deduped_cells = dedupe_detected_cells(
		all_detected_cells,
		scan_config=scan_config,
	)
	shelves = build_detected_shelves(
		deduped_cells,
		shelf_threshold_mm=max(fine_step_mm * 2.0, 5.0),
	)
	run_id, raw_scan_path, metadata_path = persist_staged_scan(
		scans_dir=scans_dir,
		side=side,
		bounds=bounds,
		fine_step_mm=fine_step_mm,
		settle_ms=settle_ms,
		samples_per_point=samples_per_point,
		coarse_scan=coarse_scan,
		candidates=candidates,
		fine_scan_results=fine_scan_results,
		wall_depth_mm=wall_depth_mm,
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
		total_points=(len(coarse_scan.x_positions) * len(coarse_scan.y_positions)) + sum(
			len(fine_scan.x_positions) * len(fine_scan.y_positions)
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


def apply_analysis(
	*,
	analysis: Any,
	grid: StorageGrid,
	grid_path: Path,
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
		recorder, side, bounds, step_mm, settle_ms, samples_per_point = load_continue_scan_context(
			args.scans_dir,
		)
		run_parameters_document = load_json_file(recorder.run_parameters_path)
		run_parameters = run_parameters_document["run_parameters"]
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
		settle_ms = args.settle_ms if args.settle_ms is not None else scan_config.settle_ms
		samples_per_point = (
			args.samples_per_point
			if args.samples_per_point is not None
			else scan_config.samples_per_point
		)
		bounds = derive_bounds(args)
		run_parameters = build_run_parameters(
			args=args,
			side=side,
			bounds=bounds,
			step_mm=step_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)

	if step_mm <= 0:
		raise ValueError("--step must be greater than zero")

	if settle_ms < 0:
		raise ValueError("--settle-ms must be zero or greater")

	if samples_per_point <= 0:
		raise ValueError("--samples-per-point must be greater than zero")

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
		f"points:{total_points}"
	)

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
					scan_config=scan_config,
					scans_dir=args.scans_dir,
					run_parameters=run_parameters,
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
