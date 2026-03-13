#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
	REPO_ROOT / "perpetual_hardware/packages/perpetual_os1/src",
	REPO_ROOT / "perpetual_hardware/packages/hardware/src",
]

for import_path in reversed(PYTHON_PATHS):
	sys.path.insert(0, str(import_path))

from perpetual_os1.common import CONFIG_PATH, load_config_file, resolve_grid_path
from perpetual_os1.laser_scan import ScanBounds
import perpetual_os1.common as common_module
import perpetual_os1.robot as robot_module
from perpetual_os1.robot import Robot
from perpetual_os1.storage import StorageGrid, StorageModuleSide


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Run a laser tray scan and print detected calibration points.")
	parser.add_argument("--side", choices=["left", "right", "left_module", "right_module"], default="left")
	parser.add_argument("--x-min", type=float)
	parser.add_argument("--x-max", type=float)
	parser.add_argument("--y-min", type=float)
	parser.add_argument("--y-max", type=float)
	parser.add_argument("--step-mm", type=float)
	parser.add_argument("--settle-ms", type=int)
	parser.add_argument("--samples-per-point", type=int)
	parser.add_argument("--stage-extension-mm", type=float)
	parser.add_argument("--scans-dir", default="data/scans")
	parser.add_argument("--config", default=str(CONFIG_PATH))
	parser.add_argument("--apply", action="store_true")
	return parser.parse_args()


def normalize_side(raw_side: str) -> StorageModuleSide:
	if raw_side in ("left", "left_module"):
		return StorageModuleSide.LEFT

	return StorageModuleSide.RIGHT


def derive_bounds(
	*,
	grid: StorageGrid,
	side: StorageModuleSide,
	step_mm: float,
	args: argparse.Namespace,
) -> ScanBounds:
	explicit_bounds = [args.x_min, args.x_max, args.y_min, args.y_max]

	if any(value is not None for value in explicit_bounds):
		if not all(value is not None for value in explicit_bounds):
			raise ValueError("x/y bounds must be provided together")

		return ScanBounds(
			x_min=float(args.x_min),
			x_max=float(args.x_max),
			y_min=float(args.y_min),
			y_max=float(args.y_max),
		)

	x_values: list[float] = []
	y_values: list[float] = []

	for shelf in grid.get_side(side):
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue

			x_values.append(float(cell.calibration_point.x))
			y_values.append(float(cell.calibration_point.y))

	if len(x_values) == 0 or len(y_values) == 0:
		raise ValueError("Scan bounds are required when the side has no calibrated cells")

	margin_mm = max(step_mm * 2.0, 20.0)
	return ScanBounds(
		x_min=float(min(x_values) - margin_mm),
		x_max=float(max(x_values) + margin_mm),
		y_min=float(min(y_values) - margin_mm),
		y_max=float(max(y_values) + margin_mm),
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


async def run_scan(args: argparse.Namespace) -> int:
	print_status("phase=config")
	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	side = normalize_side(args.side)
	step_mm = args.step_mm if args.step_mm is not None else scan_config.step_mm
	settle_ms = args.settle_ms if args.settle_ms is not None else scan_config.settle_ms
	samples_per_point = (
		args.samples_per_point
		if args.samples_per_point is not None
		else scan_config.samples_per_point
	)
	grid_path = Path(resolve_grid_path(config))
	grid = StorageGrid.load_from_file(str(grid_path)) if grid_path.exists() is True else StorageGrid()
	bounds = derive_bounds(grid=grid, side=side, step_mm=step_mm, args=args)
	total_x_points = int(round((bounds.x_max - bounds.x_min) / step_mm)) + 1
	total_y_points = int(round((bounds.y_max - bounds.y_min) / step_mm)) + 1
	total_points = total_x_points * total_y_points
	print_status(
		f"scan=side:{side.value} "
		f"bounds:x=({bounds.x_min:.2f},{bounds.x_max:.2f}) "
		f"y=({bounds.y_min:.2f},{bounds.y_max:.2f}) "
		f"step_mm:{step_mm:.2f} "
		f"points:{total_points}"
	)

	print_status("phase=robot-init")
	with tempfile.NamedTemporaryFile(
		mode="w",
		suffix=".json",
		prefix="laser-script-config-",
		dir=str(Path(args.config).resolve().parent),
		delete=False,
	) as temp_config_file:
		temp_config_path = Path(temp_config_file.name)
		temp_config_data = config.model_dump(mode="json")
		temp_config_data["robot"]["io"]["curtain"]["enabled"] = False
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
			analysis = await robot.scan_storage(
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
				scans_dir=args.scans_dir,
				apply=args.apply,
				stage_extension_mm=args.stage_extension_mm,
				progress_callback=did_progress,
			)
		finally:
			print_status("phase=robot-shutdown")
			await robot.shutdown()
	finally:
		common_module.CONFIG_PATH = original_common_config_path
		robot_module.CONFIG_PATH = original_robot_config_path
		temp_config_path.unlink(missing_ok=True)

	print_status("phase=analysis")
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
