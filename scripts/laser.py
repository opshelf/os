#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_PATHS = [
	REPO_ROOT / 'openshelf_hardware/packages/openshelf_os/src',
	REPO_ROOT / 'openshelf_hardware/packages/hardware/src',
]

for import_path in reversed(PYTHON_PATHS):
	sys.path.insert(0, str(import_path))

import openshelf_os.laser_calibration as laser_module
from openshelf_os.common import (
	CONFIG_PATH,
	load_config_file,
	resolve_grid_path,
)
from openshelf_os.laser_calibration.scan import ScanBounds, build_axis_positions
from openshelf_os.storage import StorageModuleSide

DEFAULT_SCANS_DIR_NAME = 'calibration'


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description='Run tray scan, single-cell rescan, validation, saved-run replay, or IO box scouting.'
	)
	parser.add_argument('--module', choices=['left', 'right'])
	parser.add_argument('--x', nargs=2, metavar=('MIN', 'MAX'), type=float)
	parser.add_argument('--y', nargs=2, metavar=('MIN', 'MAX'), type=float)
	parser.add_argument('--y-offset', type=float, default=0.0)
	parser.add_argument('--step', type=float)
	parser.add_argument('--settle-ms', type=int)
	parser.add_argument('--samples-per-point', type=int)
	parser.add_argument('--known-shelf-edge-y', nargs='+', type=float)
	parser.add_argument('--npy', type=Path)
	parser.add_argument('--config', default=str(CONFIG_PATH))
	parser.add_argument('--apply', action='store_true')
	parser.add_argument('--continue', dest='continue_scan', action='store_true')
	parser.add_argument('--from-run', type=Path)
	parser.add_argument('--shelf-scout-only', action='store_true')
	parser.add_argument('--rescan-cell', type=str)
	parser.add_argument('--validate-cell', type=str)
	parser.add_argument('--validate-cycles', type=int, default=1)
	parser.add_argument('--calibrate-angle', action='store_true', help=argparse.SUPPRESS)
	parser.add_argument('--calibrate-io-box', action='store_true')
	parser.add_argument('--io-box-interior-width-mm', type=float, default=laser_module.IO_BOX_INTERIOR_WIDTH_MM)
	parser.add_argument('--io-box-x-mm', type=float, default=laser_module.IO_BOX_DEFAULT_START_X_MM)
	parser.add_argument('--io-box-start-y-mm', type=float, default=laser_module.IO_BOX_DEFAULT_START_Y_MM)
	parser.add_argument('--io-box-y-min-mm', type=float)
	parser.add_argument('--io-box-x-max-mm', type=float, default=laser_module.IO_BOX_DEFAULT_X_MAX_MM)
	parser.add_argument('--io-box-coarse-step-mm', type=float, default=laser_module.IO_BOX_COARSE_STEP_MM)
	parser.add_argument('--io-box-fine-step-mm', type=float, default=laser_module.IO_BOX_FINE_STEP_MM)
	parser.add_argument('--io-box-fine-margin-mm', type=float, default=laser_module.IO_BOX_FINE_MARGIN_MM)
	parser.add_argument('--x-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, help=argparse.SUPPRESS)
	parser.add_argument('--y-mm', type=float, help=argparse.SUPPRESS)
	parser.add_argument('--z-offset-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, help=argparse.SUPPRESS)
	parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, help=argparse.SUPPRESS)
	parser.add_argument('--rotation-center-deg', type=float, help=argparse.SUPPRESS)
	parser.add_argument('--stage-extension-mm', type=float, help=argparse.SUPPRESS)
	parser.add_argument('--base-z-mm', type=float, help=argparse.SUPPRESS)
	parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	parser.add_argument('--laser-offset-x-mm', type=float)
	parser.add_argument('--laser-offset-z-mm', type=float, help=argparse.SUPPRESS)
	parser.add_argument('--beam-zero-deg', type=float, default=0.0, help=argparse.SUPPRESS)
	parser.add_argument('--alpha-min-deg', type=float, default=-10.0, help=argparse.SUPPRESS)
	parser.add_argument('--alpha-max-deg', type=float, default=10.0, help=argparse.SUPPRESS)
	parser.add_argument('--coarse-step-deg', type=float, default=0.1, help=argparse.SUPPRESS)
	parser.add_argument('--fine-window-deg', type=float, default=0.3, help=argparse.SUPPRESS)
	parser.add_argument('--fine-step-deg', type=float, default=0.005, help=argparse.SUPPRESS)
	parser.add_argument('--output', type=Path, help=argparse.SUPPRESS)
	return parser


def normalize_module(raw_module: str) -> StorageModuleSide:
	if raw_module == 'left':
		return StorageModuleSide.LEFT

	return StorageModuleSide.RIGHT


def get_current_shelf_stage_extension_mm(config: Any, side: StorageModuleSide) -> float:
	parameters = config.robot.tool.parameters

	if side == StorageModuleSide.LEFT:
		return float(getattr(parameters, 'STAGE_EXTENSION_SHELF_FRONT', 43.0))

	return float(getattr(parameters, 'STAGE_EXTENSION_SHELF_BACK', 43.0))


def parse_single_cell_rescan_target(
	raw_value: str | None,
) -> laser_module.SingleCellRescanTarget | None:
	if raw_value is None:
		return None

	target_parts = raw_value.split(':')

	if len(target_parts) != 2:
		raise ValueError('--rescan-cell must be SHELF_IDX:CELL_IDX')

	try:
		shelf_idx = int(target_parts[0])
		cell_idx = int(target_parts[1])
	except ValueError as exc:
		raise ValueError('--rescan-cell must be SHELF_IDX:CELL_IDX') from exc

	if shelf_idx < 0 or cell_idx < 0:
		raise ValueError('--rescan-cell indices must be non-negative')

	return laser_module.SingleCellRescanTarget(
		shelf_idx=shelf_idx,
		cell_idx=cell_idx,
	)


def derive_bounds(args: argparse.Namespace) -> ScanBounds:
	x_min, x_max = args.x
	y_min, y_max = args.y

	if x_max < x_min:
		raise ValueError('--x requires MIN <= MAX')

	if y_max < y_min:
		raise ValueError('--y requires MIN <= MAX')

	return ScanBounds(
		x_min=float(x_min),
		x_max=float(x_max),
		y_min=float(y_min) + float(args.y_offset),
		y_max=float(y_max) + float(args.y_offset),
	)


def print_analysis(analysis: Any) -> None:
	print(f'run_id: {analysis.run_id}')
	print(f'side: {analysis.side.value}')
	print(
		'bounds: '
		f'x=({analysis.bounds.x_min:.2f}, {analysis.bounds.x_max:.2f}) '
		f'y=({analysis.bounds.y_min:.2f}, {analysis.bounds.y_max:.2f})'
	)
	print(f'wall_depth_mm: {analysis.wall_depth_mm:.2f}')
	print(f'raw_scan_path: {analysis.raw_scan_path}')

	total_cells = sum(len(shelf.cells) for shelf in analysis.shelves)
	print(f'detected_shelves: {len(analysis.shelves)}')
	print(f'detected_cells: {total_cells}')

	if total_cells == 0:
		print('no calibration points found')
		return

	for shelf in analysis.shelves:
		for cell in shelf.cells:
			print(
				f'shelf={cell.shelf_idx} '
				f'cell={cell.cell_idx} '
				f'tool_x={cell.tool_x_mm:.2f} '
				f'tool_y={cell.tool_y_mm:.2f} '
				f'center_x={cell.center_x_mm:.2f} '
				f'center_y={cell.center_y_mm:.2f} '
				f'width={cell.width_mm:.2f} '
				f'height={cell.height_mm:.2f}'
			)


def derive_scans_dir(config_path: str) -> str:
	config_file = Path(config_path).resolve()
	return str((config_file.parent / DEFAULT_SCANS_DIR_NAME).resolve())



def validate_io_box_calibration_args(args: argparse.Namespace) -> None:
	if args.calibrate_angle is True:
		raise ValueError('--calibrate-angle cannot be used with --calibrate-io-box')

	if args.module is not None and args.module != 'left':
		raise ValueError('--calibrate-io-box only supports the left module side')

	if args.io_box_interior_width_mm <= 0.0:
		raise ValueError('--io-box-interior-width-mm must be positive')

	if args.continue_scan is True:
		raise ValueError('--continue cannot be used with --calibrate-io-box')

	if args.npy is not None:
		raise ValueError('--npy cannot be used with --calibrate-io-box')

	if args.apply is True:
		raise ValueError('--apply cannot be used with --calibrate-io-box')

	if args.rescan_cell is not None:
		raise ValueError('--rescan-cell cannot be used with --calibrate-io-box')

	if args.validate_cell is not None:
		raise ValueError('--validate-cell cannot be used with --calibrate-io-box')

	if args.shelf_scout_only is True:
		raise ValueError('--shelf-scout-only cannot be used with --calibrate-io-box')

	if args.known_shelf_edge_y is not None:
		raise ValueError('--known-shelf-edge-y cannot be used with --calibrate-io-box')

	if args.x is not None or args.y is not None:
		raise ValueError('--x/--y cannot be used with --calibrate-io-box')

	if args.step is not None:
		raise ValueError('--step cannot be used with --calibrate-io-box; use --io-box-fine-step-mm')

	if args.y_offset != 0.0:
		raise ValueError('--y-offset cannot be used with --calibrate-io-box')

	if args.x_range is not None or args.y_mm is not None:
		raise ValueError('--x-range/--y-mm cannot be used with --calibrate-io-box')

	if args.z_offset_range is not None or args.rotation_range is not None:
		raise ValueError('--z-offset-range/--rotation-range cannot be used with --calibrate-io-box')

	if args.output is not None:
		raise ValueError('--output cannot be used with --calibrate-io-box')

	if args.io_box_coarse_step_mm <= 0.0:
		raise ValueError('--io-box-coarse-step-mm must be positive')

	if args.io_box_fine_step_mm <= 0.0:
		raise ValueError('--io-box-fine-step-mm must be positive')

	if args.io_box_fine_margin_mm < 0.0:
		raise ValueError('--io-box-fine-margin-mm must be zero or greater')

	if args.io_box_start_y_mm <= 0.0:
		raise ValueError('--io-box-start-y-mm must be positive')

	if args.io_box_x_max_mm <= args.io_box_x_mm:
		raise ValueError('--io-box-x-max-mm must be greater than --io-box-x-mm')

	if args.io_box_y_min_mm is not None and args.io_box_start_y_mm <= args.io_box_y_min_mm:
		raise ValueError('--io-box-start-y-mm must be greater than --io-box-y-min-mm')


async def run_io_box_calibration(
	*,
	args: argparse.Namespace,
	config: Any,
) -> int:
	validate_io_box_calibration_args(args)
	scan_config = config.robot.grid.scan
	settle_ms = (
		args.settle_ms
		if args.settle_ms is not None
		else laser_module.get_fine_settle_ms(
			laser_module.get_laser_sample_settle_ms(scan_config)
		)
	)
	samples_per_point = (
		args.samples_per_point
		if args.samples_per_point is not None
		else laser_module.get_fine_samples_per_point(
			laser_module.get_laser_samples_per_point(scan_config)
		)
	)
	y_min_mm = (
		float(args.io_box_y_min_mm)
		if args.io_box_y_min_mm is not None
		else float(getattr(scan_config, 'left_bounds_y_min_mm', 0.0))
	)
	laser_offset_x_mm = (
		float(args.laser_offset_x_mm)
		if args.laser_offset_x_mm is not None
		else laser_module.require_scan_config_parameter(scan_config, 'laser_offset_x_mm')
	)
	laser_offset_y_mm = laser_module.require_scan_config_parameter(scan_config, 'laser_offset_y_mm')

	laser_module.print_status('phase=io-box-init')
	laser_module.print_status(
		f'io_box_setup x={float(args.io_box_x_mm):.2f} '
		f'y=({float(args.io_box_start_y_mm):.2f},{y_min_mm:.2f}) '
		f'x_max={float(args.io_box_x_max_mm):.2f} '
		f'width={float(args.io_box_interior_width_mm):.2f} '
		f'coarse_step={float(args.io_box_coarse_step_mm):.2f} '
		f'fine_step={float(args.io_box_fine_step_mm):.2f} '
		f'laser_offset_x_mm={laser_offset_x_mm:.2f} '
		f'settle_ms={settle_ms} samples_per_point={samples_per_point}'
	)

	async def didRunIoBoxCalibration(robot: Any) -> laser_module.IOBoxScoutResult:
		return await laser_module.run_io_box_scout(
			robot=robot,
			scan_config=scan_config,
			io_box_interior_width_mm=float(args.io_box_interior_width_mm),
			start_x_mm=float(args.io_box_x_mm),
			start_y_mm=float(args.io_box_start_y_mm),
			y_min_mm=y_min_mm,
			x_max_mm=float(args.io_box_x_max_mm),
			coarse_step_mm=float(args.io_box_coarse_step_mm),
			fine_step_mm=float(args.io_box_fine_step_mm),
			fine_margin_mm=float(args.io_box_fine_margin_mm),
			settle_ms=int(settle_ms),
			samples_per_point=int(samples_per_point),
			laser_offset_x_mm=laser_offset_x_mm,
			laser_offset_y_mm=laser_offset_y_mm,
		)

	result = await laser_module.run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		didRun=didRunIoBoxCalibration,
		shutdown_action=args.shutdown_action,
	)
	print_io_box_scout_result(result)
	return 0


def print_io_box_scout_result(result: laser_module.IOBoxScoutResult) -> None:
	measured_x_min_label = 'none'

	if result.measured_x_min_mm is not None:
		measured_x_min_label = f'{result.measured_x_min_mm:.2f}'

	print(f'io_box_rough_y_min_mm: {result.rough_y_min_mm:.2f}')
	print(f'io_box_rough_y_max_mm: {result.rough_y_max_mm:.2f}')
	print(f'io_box_fine_y_min_mm: {result.fine_y_min_mm:.2f}')
	print(f'io_box_fine_y_max_mm: {result.fine_y_max_mm:.2f}')
	print(f'io_box_measured_x_min_mm: {measured_x_min_label}')
	print(f'io_box_measured_x_max_mm: {result.measured_x_max_mm:.2f}')
	print(f'io_box_derived_x_min_mm: {result.derived_x_min_mm:.2f}')
	print(f'io_box_interior_width_mm: {result.io_box_interior_width_mm:.2f}')
	print(f'io_box_laser_center_x_mm: {result.laser_center_x_mm:.2f}')
	print(f'io_box_laser_center_y_mm: {result.laser_center_y_mm:.2f}')
	print(f'io_box_laser_offset_x_mm: {result.laser_offset_x_mm:.2f}')
	print(f'io_box_laser_offset_y_mm: {result.laser_offset_y_mm:.2f}')
	print(f'io_box_axis_center_x_mm: {result.axis_center_x_mm:.2f}')
	print(f'io_box_axis_center_y_mm: {result.axis_center_y_mm:.2f}')
	print(f'io_box_low_y_offset_mm: {laser_module.IO_BOX_LOW_Y_OFFSET_MM:.2f}')
	print(f'io_box_high_y_offset_mm: -{laser_module.IO_BOX_HIGH_Y_OFFSET_MM:.2f}')
	print(f'io_box_calibration_low_x_mm: {result.calibration_low_x_mm:.2f}')
	print(f'io_box_calibration_low_y_mm: {result.calibration_low_y_mm:.2f}')
	print(f'io_box_calibration_high_x_mm: {result.calibration_high_x_mm:.2f}')
	print(f'io_box_calibration_high_y_mm: {result.calibration_high_y_mm:.2f}')
	print(f'io_box_stage_extension_front_offset_mm: {laser_module.IO_BOX_STAGE_EXTENSION_FRONT_OFFSET_MM:.2f}')
	print(f'io_box_stage_extension_mm: {result.stage_extension_mm:.2f}')
	print(f'io_box_z_extension_mm: {result.z_extension_mm:.2f}')
	print(f'io_box_final_y_refined: {result.final_y_refined}')


def load_run_parameters_for_analysis(analysis: Any) -> dict[str, Any]:
	run_parameters_path = Path(analysis.metadata_path).resolve().parent / 'run_parameters.json'

	if run_parameters_path.exists() is False:
		return {}

	document = laser_module.load_json_file(run_parameters_path)
	return dict(document.get('run_parameters', {}))


def load_reference_target(reference_apply: dict[str, Any], field_name: str) -> tuple[int, int]:
	target = reference_apply.get(field_name)

	if isinstance(target, dict) is False:
		raise ValueError(f'Saved scoped run is missing {field_name} reference apply metadata')

	return int(target['shelf_idx']), int(target['cell_idx'])


def validate_reference_apply_targets(
	*,
	reference_apply: dict[str, Any],
	scan_config: Any,
	side: StorageModuleSide,
) -> None:
	saved_xy_target = load_reference_target(reference_apply, 'xy_reference_target')
	saved_z_target = load_reference_target(reference_apply, 'z_reference_target')
	current_xy_reference = laser_module.get_scan_xy_reference(scan_config=scan_config, side=side)
	current_z_reference = laser_module.get_scan_z_reference(scan_config=scan_config, side=side)
	current_xy_target = (int(current_xy_reference.shelf_idx), int(current_xy_reference.cell_idx))
	current_z_target = (int(current_z_reference[0]), int(current_z_reference[1]))

	if saved_xy_target != current_xy_target or saved_z_target != current_z_target:
		raise ValueError(
			'Saved scoped run used different reference targets and cannot be applied to the current config'
		)


def load_reference_apply_data(
	*,
	run_parameters: dict[str, Any],
	scan_config: Any,
	side: StorageModuleSide,
) -> tuple[Any, float]:
	reference_apply = run_parameters.get('reference_apply')

	if isinstance(reference_apply, dict) is False:
		raise ValueError('Saved scoped run is missing reference apply metadata')

	validate_reference_apply_targets(
		reference_apply=reference_apply,
		scan_config=scan_config,
		side=side,
	)
	xy_reference_detected_cell = reference_apply.get('xy_reference_detected_cell')
	z_reference_wall_depth_mm = reference_apply.get('z_reference_wall_depth_mm')

	if isinstance(xy_reference_detected_cell, dict) is False:
		raise ValueError('Saved scoped run is missing XY reference apply metadata')

	if z_reference_wall_depth_mm is None:
		raise ValueError('Saved scoped run is missing Z reference apply metadata')

	return laser_module.ScanDetectedCell.model_validate(xy_reference_detected_cell), float(z_reference_wall_depth_mm)


def apply_loaded_run_analysis(
	*,
	analysis: Any,
	config: Any,
	grid: Any,
	grid_path: Path,
	run_parameters: dict[str, Any],
) -> None:
	auto_calibration_scope = run_parameters.get('auto_calibration_scope')
	single_cell_rescan = run_parameters.get('single_cell_rescan')

	if isinstance(auto_calibration_scope, dict) is True:
		scope_mode = auto_calibration_scope.get('mode')

		if scope_mode == 'shelf':
			xy_reference_detected_cell, z_reference_wall_depth_mm = load_reference_apply_data(
				run_parameters=run_parameters,
				scan_config=config.robot.grid.scan,
				side=analysis.side,
			)
			laser_module.apply_single_shelf_analysis(
				analysis=analysis,
				grid=grid,
				grid_path=grid_path,
				scan_config=config.robot.grid.scan,
				side=analysis.side,
				target_shelf_idx=int(auto_calibration_scope['shelf_idx']),
				current_stage_extension_mm=get_current_shelf_stage_extension_mm(config, analysis.side),
				xy_reference_detected_cell=xy_reference_detected_cell,
				z_reference_wall_depth_mm=z_reference_wall_depth_mm,
			)
			return

	if isinstance(single_cell_rescan, dict) is True:
		candidate_data = single_cell_rescan.get('candidate')

		if isinstance(candidate_data, dict) is False:
			raise ValueError('Saved single-cell run is missing candidate metadata')

		target = laser_module.SingleCellRescanTarget(
			shelf_idx=int(single_cell_rescan['shelf_idx']),
			cell_idx=int(single_cell_rescan['cell_idx']),
		)
		candidate = laser_module.GapCandidateRegion(
			x_min=float(candidate_data['x_min']),
			x_max=float(candidate_data['x_max']),
			y_min=float(candidate_data['y_min']),
			y_max=float(candidate_data['y_max']),
			wall_depth_mm=(
				None
				if candidate_data.get('wall_depth_mm') is None
				else float(candidate_data['wall_depth_mm'])
			),
		)
		matched_cell = laser_module.find_detected_cell_for_candidate(
			analysis=analysis,
			candidate=candidate,
		)

		if matched_cell is None:
			raise ValueError('No detected cell matched saved --rescan-cell target')

		if isinstance(auto_calibration_scope, dict) is True or 'reference_apply' in run_parameters:
			xy_reference_detected_cell, z_reference_wall_depth_mm = load_reference_apply_data(
				run_parameters=run_parameters,
				scan_config=config.robot.grid.scan,
				side=analysis.side,
			)
		else:
			xy_reference_detected_cell = None
			z_reference_wall_depth_mm = None

		laser_module.apply_single_cell_rescan(
			grid=grid,
			target=target,
			side=analysis.side,
			detected_cell=matched_cell,
			wall_depth_mm=float(analysis.wall_depth_mm),
			scan_config=config.robot.grid.scan,
			current_stage_extension_mm=get_current_shelf_stage_extension_mm(config, analysis.side),
			xy_reference_detected_cell=xy_reference_detected_cell,
			z_reference_wall_depth_mm=z_reference_wall_depth_mm,
		)
		return

	laser_module.apply_analysis(
		analysis=analysis,
		grid=grid,
		grid_path=grid_path,
		scan_config=config.robot.grid.scan,
		side=analysis.side,
		current_stage_extension_mm=get_current_shelf_stage_extension_mm(config, analysis.side),
	)


async def run_scan(args: argparse.Namespace) -> int:
	if args.calibrate_angle is True:
		raise ValueError('--calibrate-angle laser-fit calibration was removed; use API auto calibration for stage-stall angle calibration')

	laser_module.print_status('phase=config')
	config = load_config_file(args.config)
	scans_dir = derive_scans_dir(args.config)
	args.scans_dir = scans_dir
	scan_config = config.robot.grid.scan
	recorder: laser_module.StagedScanRecorder | None = None
	rescan_target = parse_single_cell_rescan_target(args.rescan_cell)
	validation_target = parse_single_cell_rescan_target(args.validate_cell)
	rescan_candidate: laser_module.GapCandidateRegion | None = None
	grid_path = Path(resolve_grid_path(config))
	grid = laser_module.load_grid(grid_path)

	if args.from_run is not None:
		if args.continue_scan is True:
			raise ValueError('--from-run cannot be used with --continue')

		if args.npy is not None:
			raise ValueError('--from-run cannot be used with --npy')

		if args.calibrate_io_box is True:
			raise ValueError('--from-run cannot be used with --calibrate-io-box')


		if args.rescan_cell is not None or args.validate_cell is not None:
			raise ValueError('--from-run cannot be used with --rescan-cell or --validate-cell')

		laser_module.print_status('phase=load-run')
		analysis = laser_module.load_staged_scan_analysis(
			args.from_run,
			scan_config=scan_config,
		)
		run_parameters = load_run_parameters_for_analysis(analysis)

		if args.apply is True:
			laser_module.print_status('phase=apply')
			apply_loaded_run_analysis(
				analysis=analysis,
				config=config,
				grid=grid,
				grid_path=grid_path,
				run_parameters=run_parameters,
			)

		print_analysis(analysis)
		run_parameters_path = Path(analysis.metadata_path).resolve().parent / 'run_parameters.json'

		if run_parameters_path.exists() is True:
			print(f'run_parameters_path: {run_parameters_path}')

		return 0

	if args.calibrate_io_box is True:
		return await run_io_box_calibration(args=args, config=config)


	if rescan_target is not None and validation_target is not None:
		raise ValueError('--rescan-cell cannot be used with --validate-cell')

	if validation_target is not None:
		if args.continue_scan is True:
			raise ValueError('--validate-cell cannot be used with --continue')

		if args.npy is not None:
			raise ValueError('--validate-cell cannot be used with --npy')

		if args.module is None:
			raise ValueError('--module is required with --validate-cell')

		if args.apply is True:
			raise ValueError('--apply cannot be used with --validate-cell')

		if args.x is not None or args.y is not None:
			raise ValueError('--x/--y cannot be used with --validate-cell')

		if args.step is not None:
			raise ValueError('--step cannot be used with --validate-cell')

		if args.shelf_scout_only is True:
			raise ValueError('--shelf-scout-only cannot be used with --validate-cell')

		if args.known_shelf_edge_y is not None:
			raise ValueError('--known-shelf-edge-y cannot be used with --validate-cell')

		if args.validate_cycles <= 0:
			raise ValueError('--validate-cycles must be greater than zero')

		side = normalize_module(args.module)
		laser_module.print_status('phase=validation-init')

		async def didRunValidation(robot: Any) -> None:
			await laser_module.run_fixed_xy_validation(
				robot=robot,
				grid=grid,
				side=side,
				target=validation_target,
				cycles=args.validate_cycles,
			)

		await laser_module.run_with_temp_robot_config(
			config=config,
			config_path=args.config,
			didRun=didRunValidation,
			shutdown_action='stop',
		)

		return 0

	if args.continue_scan is True:
		if rescan_target is not None:
			raise ValueError('--rescan-cell cannot be used with --continue')

		(
			recorder,
			side,
			bounds,
			step_mm,
			settle_ms,
			samples_per_point,
			coarse_settle_ms,
			coarse_samples_per_point,
		) = laser_module.load_continue_scan_context(scans_dir)
		run_parameters_document = laser_module.load_json_file(recorder.run_parameters_path)
		run_parameters = run_parameters_document['run_parameters']
		shelf_scout_only = bool(run_parameters.get('cli', {}).get('shelf_scout_only', False))
		known_shelf_edge_y = run_parameters.get('cli', {}).get('known_shelf_edge_y')
		laser_module.print_status(f'resume_run_id={recorder.run_id}')
	else:
		if args.module is None:
			raise ValueError('--module is required unless --continue is used')

		if args.step is None:
			raise ValueError('--step is required unless --continue is used')

		if rescan_target is None and args.x is None:
			raise ValueError('--x is required unless --continue or --rescan-cell is used')

		if rescan_target is None and args.y is None:
			raise ValueError('--y is required unless --continue or --rescan-cell is used')

		if rescan_target is not None and args.shelf_scout_only is True:
			raise ValueError('--shelf-scout-only cannot be used with --rescan-cell')

		if rescan_target is not None and args.known_shelf_edge_y is not None:
			raise ValueError('--known-shelf-edge-y cannot be used with --rescan-cell')

		side = normalize_module(args.module)
		step_mm = float(args.step)
		settle_ms = (
			args.settle_ms
			if args.settle_ms is not None
			else laser_module.get_fine_settle_ms(
				laser_module.get_laser_sample_settle_ms(scan_config)
			)
		)
		samples_per_point = (
			args.samples_per_point
			if args.samples_per_point is not None
			else laser_module.get_fine_samples_per_point(
				laser_module.get_laser_samples_per_point(scan_config)
			)
		)

		coarse_settle_ms = laser_module.get_coarse_settle_ms(settle_ms)
		coarse_samples_per_point = laser_module.get_coarse_samples_per_point(samples_per_point)
		shelf_scout_only = bool(args.shelf_scout_only)
		known_shelf_edge_y = (
			[float(value) for value in args.known_shelf_edge_y]
			if args.known_shelf_edge_y is not None
			else None
		)

		if rescan_target is not None:
			shelf_scout_only = False
			known_shelf_edge_y = None
			rescan_candidate = laser_module.build_single_cell_rescan_candidate(
				grid=grid,
				side=side,
				target=rescan_target,
				scan_config=scan_config,
			)

			if args.x is None and args.y is None:
				bounds = laser_module.build_single_cell_rescan_bounds(
					candidate=rescan_candidate,
					scan_config=scan_config,
				)
			elif args.x is not None and args.y is not None:
				bounds = derive_bounds(args)
			else:
				raise ValueError('--x and --y must both be set when overriding --rescan-cell bounds')

			candidate_center_x_mm, candidate_center_y_mm = laser_module.get_candidate_center(
				rescan_candidate
			)

			if laser_module.is_point_in_bounds(
				bounds=bounds,
				x_mm=candidate_center_x_mm,
				y_mm=candidate_center_y_mm,
			) is False:
				raise ValueError('--rescan-cell target is outside the provided scan bounds')

			wall_depth_label = (
				f'{float(rescan_candidate.wall_depth_mm):.2f}'
				if rescan_candidate.wall_depth_mm is not None
				else 'scan-estimated'
			)
			laser_module.print_status(
				f'single_cell_rescan=target:{rescan_target.shelf_idx}:{rescan_target.cell_idx} '
				f'center_x={candidate_center_x_mm:.2f} center_y={candidate_center_y_mm:.2f} '
				f'wall_depth_mm={wall_depth_label}'
			)
		else:
			bounds = derive_bounds(args)

		run_parameters = laser_module.build_run_parameters(
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

		if rescan_target is not None and rescan_candidate is not None:
			run_parameters['single_cell_rescan'] = {
				'shelf_idx': int(rescan_target.shelf_idx),
				'cell_idx': int(rescan_target.cell_idx),
				'candidate': {
					'x_min': float(rescan_candidate.x_min),
					'x_max': float(rescan_candidate.x_max),
					'y_min': float(rescan_candidate.y_min),
					'y_max': float(rescan_candidate.y_max),
					'wall_depth_mm': (
						float(rescan_candidate.wall_depth_mm)
						if rescan_candidate.wall_depth_mm is not None
						else None
					),
				},
			}

	if step_mm <= 0:
		raise ValueError('--step must be greater than zero')

	if settle_ms < 0:
		raise ValueError('--settle-ms must be zero or greater')

	if samples_per_point <= 0:
		raise ValueError('--samples-per-point must be greater than zero')

	if coarse_settle_ms < 0:
		raise ValueError('coarse settle must be zero or greater')

	if coarse_samples_per_point <= 0:
		raise ValueError('coarse samples per point must be greater than zero')

	x_positions = build_axis_positions(bounds.x_min, bounds.x_max, step_mm)
	y_positions = build_axis_positions(bounds.y_min, bounds.y_max, step_mm)
	total_points = len(x_positions) * len(y_positions)
	laser_module.print_status(
		f'scan=module:{side.value} '
		f'bounds:x=({bounds.x_min:.2f},{bounds.x_max:.2f}) '
		f'y=({bounds.y_min:.2f},{bounds.y_max:.2f}) '
		f'fine_step_mm:{step_mm:.2f} '
		f'coarse_step_mm:{laser_module.COARSE_SCAN_STEP_MM:.2f} '
		f'fine_settle_ms:{settle_ms} '
		f'coarse_settle_ms:{coarse_settle_ms} '
		f'fine_samples_per_point:{samples_per_point} '
		f'coarse_samples_per_point:{coarse_samples_per_point} '
		f'points:{total_points}'
	)

	if known_shelf_edge_y is not None:
		laser_module.print_status(
			f"known_shelf_edges_y={[round(float(value), 2) for value in known_shelf_edge_y]}"
		)

	if args.npy is not None:
		if args.continue_scan is True:
			raise ValueError('--npy cannot be used with --continue')

		if rescan_target is not None:
			raise ValueError('--npy cannot be used with --rescan-cell')

		laser_module.print_status('phase=load-npy')
		depth_rows = laser_module.load_depth_rows_from_npy(
			args.npy,
			expected_row_count=len(y_positions),
			expected_column_count=len(x_positions),
		)
		laser_module.print_status(f'npy={args.npy}')
		laser_module.print_status('phase=analysis')
		analysis = laser_module.analyze_depth_rows(
			side=side,
			bounds=bounds,
			step_mm=step_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			scan_config=scan_config,
			scans_dir=scans_dir,
			depth_rows=depth_rows,
		)
	else:
		laser_module.print_status('phase=robot-init')

		async def didRunScan(robot: Any) -> Any:
			laser_module.print_status('phase=scan-start')
			return await laser_module.run_staged_scan(
				robot=robot,
				side=side,
				bounds=bounds,
				fine_step_mm=step_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				coarse_settle_ms=coarse_settle_ms,
				coarse_samples_per_point=coarse_samples_per_point,
				scan_config=scan_config,
				scans_dir=scans_dir,
				run_parameters=run_parameters,
				shelf_scout_only=shelf_scout_only,
				known_shelf_edge_y=known_shelf_edge_y,
				seeded_candidates=[rescan_candidate] if rescan_candidate is not None else None,
				seeded_wall_depth_mm=(
					float(rescan_candidate.wall_depth_mm)
					if rescan_candidate is not None and rescan_candidate.wall_depth_mm is not None
					else None
				),
				recorder=recorder,
			)

		analysis = await laser_module.run_with_temp_robot_config(
			config=config,
			config_path=args.config,
			didRun=didRunScan,
			shutdown_action='shutdown',
		)

	run_parameters_path = laser_module.persist_run_parameters(
		run_dir=Path(analysis.raw_scan_path).resolve().parent,
		run_id=analysis.run_id,
		run_parameters=run_parameters,
	)

	if args.apply is True:
		laser_module.print_status('phase=apply')

		if rescan_target is not None:
			if rescan_candidate is None:
				raise ValueError('Single-cell candidate resolution failed')

			matched_cell = laser_module.find_detected_cell_for_candidate(
				analysis=analysis,
				candidate=rescan_candidate,
			)

			if matched_cell is None:
				raise ValueError('No detected cell matched --rescan-cell target')

			laser_module.apply_single_cell_rescan(
				grid=grid,
				target=rescan_target,
				side=side,
				detected_cell=matched_cell,
				wall_depth_mm=float(analysis.wall_depth_mm),
				scan_config=scan_config,
				current_stage_extension_mm=get_current_shelf_stage_extension_mm(config, side),
			)
		else:
			laser_module.apply_analysis(
				analysis=analysis,
				grid=grid,
				grid_path=grid_path,
				scan_config=scan_config,
				side=side,
				current_stage_extension_mm=get_current_shelf_stage_extension_mm(config, side),
			)

	print_analysis(analysis)
	print(f'run_parameters_path: {run_parameters_path}')
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = build_parser()

	if argv is None:
		argv = sys.argv[1:]

	if len(argv) == 0:
		parser.print_help()
		return 0

	args = parser.parse_args(argv)

	try:
		return asyncio.run(run_scan(args))
	except KeyboardInterrupt:
		return 130
	except Exception as exc:
		traceback.print_exc()
		print(str(exc), file=sys.stderr)
		return 1


if __name__ == '__main__':
	raise SystemExit(main())
