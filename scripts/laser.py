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
	REPO_ROOT / 'perpetual_hardware/packages/perpetual_os1/src',
	REPO_ROOT / 'perpetual_hardware/packages/hardware/src',
]

for import_path in reversed(PYTHON_PATHS):
	sys.path.insert(0, str(import_path))

import perpetual_os1.laser_angle_calibration as angle_module
import perpetual_os1.laser_calibration as laser_module
from perpetual_os1.common import (
	CONFIG_PATH,
	Parameters,
	load_config_file,
	resolve_grid_path,
	write_json_file_with_backup,
)
from perpetual_os1.laser_scan import ScanBounds, build_axis_positions
from perpetual_os1.storage import StorageModuleSide

DEFAULT_SCANS_DIR_NAME = 'calibration'


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description='Run tray scan, single-cell rescan, validation, angle calibration, or IO box scouting.'
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
	parser.add_argument('--shelf-scout-only', action='store_true')
	parser.add_argument('--rescan-cell', type=str)
	parser.add_argument('--validate-cell', type=str)
	parser.add_argument('--validate-cycles', type=int, default=1)
	parser.add_argument('--calibrate-angle', action='store_true')
	parser.add_argument('--calibrate-io-box', action='store_true')
	parser.add_argument('--io-box-interior-width-mm', type=float, default=laser_module.IO_BOX_INTERIOR_WIDTH_MM)
	parser.add_argument('--io-box-x-mm', type=float, default=laser_module.IO_BOX_DEFAULT_START_X_MM)
	parser.add_argument('--io-box-start-y-mm', type=float, default=laser_module.IO_BOX_DEFAULT_START_Y_MM)
	parser.add_argument('--io-box-y-min-mm', type=float)
	parser.add_argument('--io-box-x-max-mm', type=float, default=laser_module.IO_BOX_DEFAULT_X_MAX_MM)
	parser.add_argument('--io-box-coarse-step-mm', type=float, default=laser_module.IO_BOX_COARSE_STEP_MM)
	parser.add_argument('--io-box-fine-step-mm', type=float, default=laser_module.IO_BOX_FINE_STEP_MM)
	parser.add_argument('--io-box-fine-margin-mm', type=float, default=laser_module.IO_BOX_FINE_MARGIN_MM)
	parser.add_argument('--x-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float)
	parser.add_argument('--y-mm', type=float)
	parser.add_argument('--z-offset-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float)
	parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float)
	parser.add_argument('--rotation-center-deg', type=float)
	parser.add_argument('--stage-extension-mm', type=float)
	parser.add_argument('--base-z-mm', type=float)
	parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	parser.add_argument('--laser-offset-x-mm', type=float)
	parser.add_argument('--laser-offset-z-mm', type=float)
	parser.add_argument('--beam-zero-deg', type=float, default=0.0)
	parser.add_argument('--alpha-min-deg', type=float, default=-10.0)
	parser.add_argument('--alpha-max-deg', type=float, default=10.0)
	parser.add_argument('--coarse-step-deg', type=float, default=0.1)
	parser.add_argument('--fine-window-deg', type=float, default=0.3)
	parser.add_argument('--fine-step-deg', type=float, default=0.005)
	parser.add_argument('--output', type=Path)
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


def persist_config(config: Any, config_path: str) -> None:
	write_json_file_with_backup(
		Path(config_path).resolve(),
		config.model_dump(mode='json'),
		indent='\t',
		backup_group_name='config',
	)


def derive_scans_dir(config_path: str) -> str:
	config_file = Path(config_path).resolve()
	return str((config_file.parent / DEFAULT_SCANS_DIR_NAME).resolve())


def validate_angle_calibration_args(args: argparse.Namespace) -> None:
	if args.module is None:
		raise ValueError('--module is required with --calibrate-angle')

	if args.x_range is None:
		raise ValueError('--x-range is required with --calibrate-angle')

	if args.y_mm is None:
		raise ValueError('--y-mm is required with --calibrate-angle')

	if args.z_offset_range is None:
		raise ValueError('--z-offset-range is required with --calibrate-angle')

	if args.rotation_range is None:
		raise ValueError('--rotation-range is required with --calibrate-angle')

	if args.continue_scan is True:
		raise ValueError('--continue cannot be used with --calibrate-angle')

	if args.npy is not None:
		raise ValueError('--npy cannot be used with --calibrate-angle')

	if args.rescan_cell is not None:
		raise ValueError('--rescan-cell cannot be used with --calibrate-angle')

	if args.validate_cell is not None:
		raise ValueError('--validate-cell cannot be used with --calibrate-angle')

	if args.shelf_scout_only is True:
		raise ValueError('--shelf-scout-only cannot be used with --calibrate-angle')

	if args.known_shelf_edge_y is not None:
		raise ValueError('--known-shelf-edge-y cannot be used with --calibrate-angle')

	if args.x is not None or args.y is not None:
		raise ValueError('--x/--y cannot be used with --calibrate-angle; use --x-range and --y-mm')

	if args.step is not None:
		raise ValueError('--step cannot be used with --calibrate-angle; use --x-range/--rotation-range')

	if args.y_offset != 0.0:
		raise ValueError('--y-offset cannot be used with --calibrate-angle')

	if args.calibrate_io_box is True:
		raise ValueError('--calibrate-io-box cannot be used with --calibrate-angle')

	if args.alpha_max_deg < args.alpha_min_deg:
		raise ValueError('--alpha-min-deg must be <= --alpha-max-deg')

	if args.coarse_step_deg <= 0.0:
		raise ValueError('--coarse-step-deg must be positive')

	if args.fine_window_deg <= 0.0:
		raise ValueError('--fine-window-deg must be positive')

	if args.fine_step_deg <= 0.0:
		raise ValueError('--fine-step-deg must be positive')

	args.x_range = angle_module.parse_range_argument(args.x_range)
	args.z_offset_range = angle_module.parse_range_argument(args.z_offset_range)
	args.rotation_range = angle_module.parse_range_argument(args.rotation_range)


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


def apply_angle_calibration_result(
	*,
	config: Any,
	config_path: str,
	side: StorageModuleSide,
	alpha_deg: float,
) -> None:
	rotation_param = 'ROTATION_FRONT'

	if side == StorageModuleSide.RIGHT:
		rotation_param = 'ROTATION_BACK'

	current_rotation_deg = float(getattr(config.robot.tool.parameters, rotation_param))
	updated_rotation_deg = current_rotation_deg + alpha_deg
	params_data = config.robot.tool.parameters.model_dump()
	params_data[rotation_param] = updated_rotation_deg
	config.robot.tool.parameters = Parameters.model_validate(params_data)

	persist_config(config, config_path)

	laser_module.print_status(
		f'angle_calibration_apply=updated param={rotation_param} '
		f'previous_deg={current_rotation_deg:.4f} alpha_deg={alpha_deg:.4f} '
		f'updated_deg={updated_rotation_deg:.4f}'
	)


async def run_angle_calibration(
	*,
	args: argparse.Namespace,
	config: Any,
) -> int:
	validate_angle_calibration_args(args)
	scan_config = config.robot.grid.scan
	side = normalize_module(args.module)
	laser_offset_x_mm = (
		float(args.laser_offset_x_mm)
		if args.laser_offset_x_mm is not None
		else laser_module.require_scan_config_parameter(scan_config, 'laser_offset_x_mm')
	)
	laser_offset_z_mm = (
		float(args.laser_offset_z_mm)
		if args.laser_offset_z_mm is not None
		else 0.0
	)
	beam_zero_deg = float(args.beam_zero_deg)

	if args.rotation_center_deg is None:
		rotation_param = 'ROTATION_FRONT'

		if side == StorageModuleSide.RIGHT:
			rotation_param = 'ROTATION_BACK'

		args.rotation_center_deg = float(getattr(config.robot.tool.parameters, rotation_param))

	laser_module.print_status('phase=angle-calibration')
	laser_module.print_status(
		f'angle_calibration_setup module={side.value} '
		f'x_range={args.x_range} y_mm={float(args.y_mm):.3f} '
		f'z_offset_range={args.z_offset_range} rotation_range={args.rotation_range} '
		f'laser_offset_x_mm={laser_offset_x_mm:.3f} laser_offset_z_mm={laser_offset_z_mm:.3f} '
		f'beam_zero_deg={beam_zero_deg:.3f}'
	)

	samples, capture_metadata = await angle_module.capture_samples_with_robot(args)
	fit_result = angle_module.search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=laser_offset_x_mm,
		laser_offset_z_mm=laser_offset_z_mm,
		beam_zero_deg=beam_zero_deg,
	)
	angle_module.print_fit_result(fit_result)

	if args.output is not None:
		angle_module.write_output_document(
			output_path=args.output,
			mode='capture',
			samples=samples,
			fit_result=fit_result,
			metadata={
				**capture_metadata,
				'laser_offset_x_mm': laser_offset_x_mm,
				'laser_offset_z_mm': laser_offset_z_mm,
				'beam_zero_deg': beam_zero_deg,
			},
		)
		laser_module.print_status(f'angle_calibration_output={args.output}')

	if args.apply is True:
		apply_angle_calibration_result(
			config=config,
			config_path=args.config,
			side=side,
			alpha_deg=float(fit_result.alpha_deg),
		)

	return 0


async def run_scan(args: argparse.Namespace) -> int:
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

	if args.calibrate_io_box is True:
		return await run_io_box_calibration(args=args, config=config)

	if args.calibrate_angle is True:
		return await run_angle_calibration(args=args, config=config)

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
