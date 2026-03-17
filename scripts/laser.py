#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import numpy as np
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Optional

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
LASER_SCAN_Z_EXTENSION_MM = 10.0
LASER_SCAN_Y_OFFSET_MM = -10.0


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run a laser tray scan and print detected calibration points.")
	parser.add_argument("--module", choices=["left", "right"], required=True)
	parser.add_argument("--x", nargs=2, metavar=("MIN", "MAX"), type=float, required=True)
	parser.add_argument("--y", nargs=2, metavar=("MIN", "MAX"), type=float, required=True)
	parser.add_argument("--step", type=float, required=True)
	parser.add_argument("--settle-ms", type=int)
	parser.add_argument("--samples-per-point", type=int)
	parser.add_argument("--npy", type=Path)
	parser.add_argument("--scans-dir", default="/tmp")
	parser.add_argument("--config", default=str(CONFIG_PATH))
	parser.add_argument("--apply", action="store_true")
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
		y_min=float(y_min) + LASER_SCAN_Y_OFFSET_MM,
		y_max=float(y_max) + LASER_SCAN_Y_OFFSET_MM,
	)


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


async def capture_laser_scan(
	*,
	robot: Robot,
	side: StorageModuleSide,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	progress_callback: Optional[Any] = None,
) -> list[list[float | None]]:
	tool = robot._tool

	if tool._laser_enabled is False or tool._laser is None:
		raise ValueError("Laser is not enabled")

	if tool._mx is None or tool._my is None:
		raise ValueError("Gantry motors (x_axis, y_axis) are required for laser scan")

	if tool._ms is None or tool._mg is None:
		raise ValueError("Tool motors (stage, z_axis) are required for laser scan")

	if samples_per_point <= 0:
		raise ValueError("samples_per_point must be greater than zero")

	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)
	total_points = len(x_positions) * len(y_positions)
	completed_points = 0
	side_number = 0
	target_position = "front"

	if side == StorageModuleSide.RIGHT:
		side_number = 1
		target_position = "back"

	stage_extension_mm = LASER_SCAN_STAGE_EXTENSION_MM
	z_extension_mm = LASER_SCAN_Z_EXTENSION_MM
	z_move_rpm = await tool.get_param("Z_MOVE_RPM")
	z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

	if z_move_rpm is None or z_move_accel is None:
		raise ValueError("Z_MOVE_RPM and Z_MOVE_ACCEL parameters are required for laser scan")

	scan_x_mm, scan_y_mm = await get_scan_position(tool)
	rows: list[list[float | None]] = []

	print_status(
		f"scan_setup=module:{side.value} "
		f"scan_position:({scan_x_mm:.2f},{scan_y_mm:.2f}) "
		f"stage_extension_mm:{stage_extension_mm:.2f} "
		f"z_extension_mm:{z_extension_mm:.2f}"
	)

	try:
		await tool.retract()
		await tool.move(AxisCoordinate(x=scan_x_mm, y=scan_y_mm, s=side_number))
		await tool.rotate_to_position(target_position)
		await tool._ms.ready()
		await tool._ms.move_to_position(stage_extension_mm)
		await tool._mg.move_to_position(z_extension_mm, rpm=z_move_rpm, accel=z_move_accel)

		for row_idx, y_position in enumerate(y_positions):
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

				if settle_ms > 0:
					await asyncio.sleep(settle_ms / 1000.0)

				sample_values: list[float] = []

				for _ in range(samples_per_point):
					depth_mm = await tool.get_laser_distance()

					if depth_mm is not None:
						sample_values.append(float(depth_mm))

				if len(sample_values) > 0:
					sample_values.sort()
					middle_idx = len(sample_values) // 2
					row_values[column_idx] = sample_values[middle_idx]

				completed_points += 1

				if progress_callback is not None:
					callback_result = progress_callback(
						completed_points,
						total_points,
						x_position,
						y_position,
						row_values[column_idx],
					)

					if hasattr(callback_result, "__await__") is True:
						await callback_result

			rows.append(row_values)
	finally:
		await tool.retract()

	return rows


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
	side = normalize_module(args.module)
	step_mm = float(args.step)
	settle_ms = args.settle_ms if args.settle_ms is not None else scan_config.settle_ms
	samples_per_point = (
		args.samples_per_point
		if args.samples_per_point is not None
		else scan_config.samples_per_point
	)

	if step_mm <= 0:
		raise ValueError("--step must be greater than zero")

	if settle_ms < 0:
		raise ValueError("--settle-ms must be zero or greater")

	if samples_per_point <= 0:
		raise ValueError("--samples-per-point must be greater than zero")

	grid_path = Path(resolve_grid_path(config))
	grid = load_grid(grid_path)
	bounds = derive_bounds(args)
	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)
	total_points = len(x_positions) * len(y_positions)
	print_status(
		f"scan=module:{side.value} "
		f"bounds:x=({bounds.x_min:.2f},{bounds.x_max:.2f}) "
		f"y=({bounds.y_min:.2f},{bounds.y_max:.2f}) "
		f"step_mm:{step_mm:.2f} "
		f"points:{total_points}"
	)

	if args.npy is not None:
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
				last_reported_points = 0

				async def did_progress(
					completed_points: int,
					total_points: int,
					current_x_mm: float,
					current_y_mm: float,
					current_depth_mm: float | None,
				) -> None:
					nonlocal last_reported_points

					if completed_points < total_points and completed_points - last_reported_points < 25:
						return

					last_reported_points = completed_points
					depth_label = "none" if current_depth_mm is None else f"{current_depth_mm:.2f}"
					print_status(
						f"progress={completed_points}/{total_points} "
						f"x={current_x_mm:.2f} "
						f"y={current_y_mm:.2f} "
						f"depth={depth_label}"
					)

				print_status("phase=scan-start")
				depth_rows = await capture_laser_scan(
					robot=robot,
					side=side,
					bounds=bounds,
					step_mm=step_mm,
					settle_ms=settle_ms,
					samples_per_point=samples_per_point,
					progress_callback=did_progress,
				)

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
			finally:
				print_status("phase=robot-shutdown")
				await robot.shutdown()
		finally:
			common_module.CONFIG_PATH = original_common_config_path
			robot_module.CONFIG_PATH = original_robot_config_path
			temp_config_path.unlink(missing_ok=True)

	if args.apply is True:
		print_status("phase=apply")
		apply_analysis(
			analysis=analysis,
			grid=grid,
			grid_path=grid_path,
			side=side,
		)

	print_analysis(analysis)
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
