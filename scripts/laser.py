#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from dataclasses import replace
from pathlib import Path
from statistics import median
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
)
from openshelf_os.laser_calibration.scan import ScanBounds, build_axis_positions
from openshelf_os.runtime_state import RuntimeStatePaths, ensure_mode_workspace, paths_for_config_file
from openshelf_os.storage import StorageModuleSide

def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description='Run tray scan, single-cell rescan, or validation.'
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
	parser.add_argument(
		'--coarse-scan-style',
		choices=['stepped', 'flying'],
		help='Coarse scan motion. stepped stops at every grid point; flying drives the axis at a constant feed and bins samples onto the same grid.',
	)
	parser.add_argument('--coarse-scan-rpm', type=int)
	parser.add_argument('--coarse-scan-bin-mode', choices=['nearest', 'median'])
	parser.add_argument(
		'--fine-scan-style',
		choices=['stepped', 'flying'],
		help='Fine line probes. flying prefetches each line in one pass and replays the same probe logic.',
	)
	parser.add_argument('--fine-scan-feed-mm-s', type=float)
	parser.add_argument(
		'--rescan-shelf',
		type=int,
		metavar='SHELF_IDX',
		help='Rescan one shelf: bounds and known shelf edge come from the storage grid and scan config.',
	)
	parser.add_argument('--rescan-cell', type=str)
	parser.add_argument('--validate-cell', type=str)
	parser.add_argument('--validate-cycles', type=int, default=1)
	return parser


def normalize_module(raw_module: str) -> StorageModuleSide:
	if raw_module == 'left':
		return StorageModuleSide.LEFT

	return StorageModuleSide.RIGHT


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


def apply_scan_config_overrides(scan_config: Any, args: argparse.Namespace) -> Any:
	overrides: dict[str, Any] = {}

	if args.coarse_scan_style is not None:
		overrides['coarse_scan_style'] = args.coarse_scan_style

	if args.coarse_scan_rpm is not None:
		overrides['coarse_scan_rpm'] = int(args.coarse_scan_rpm)

	if args.coarse_scan_bin_mode is not None:
		overrides['coarse_scan_bin_mode'] = args.coarse_scan_bin_mode

	if args.fine_scan_style is not None:
		overrides['fine_scan_style'] = args.fine_scan_style

	if args.fine_scan_feed_mm_s is not None:
		overrides['fine_scan_feed_mm_s'] = float(args.fine_scan_feed_mm_s)

	if len(overrides) == 0:
		return scan_config

	if hasattr(scan_config, 'model_copy') is False:
		raise ValueError('--coarse-scan-* overrides require a validated laser scan config')

	return scan_config.model_copy(update=overrides)


def build_side_scan_bounds(scan_config: Any, side: StorageModuleSide) -> ScanBounds:
	prefix = 'left' if side == StorageModuleSide.LEFT else 'right'

	return ScanBounds(
		x_min=float(getattr(scan_config, f'{prefix}_bounds_x_min_mm')),
		x_max=float(getattr(scan_config, f'{prefix}_bounds_x_max_mm')),
		y_min=float(getattr(scan_config, f'{prefix}_bounds_y_min_mm')),
		y_max=float(getattr(scan_config, f'{prefix}_bounds_y_max_mm')),
	)


def shelf_calibration_center_y(shelf: Any) -> float | None:
	y_values = [
		float(cell.calibration_point.y)
		for cell in shelf.cells
		if cell.calibration_point is not None
	]

	if len(y_values) == 0:
		return None

	return float(median(y_values))


def build_single_shelf_rescan_bounds(
	*,
	base_bounds: ScanBounds,
	center_y_mm: float,
	scan_config: Any,
) -> ScanBounds:
	hole_probe_offsets_mm = laser_module.get_shelf_hole_probe_y_offsets_mm(scan_config)
	local_margin_mm = max(
		80.0,
		float(laser_module.EXPECTED_HOLE_HEIGHT_MM)
		+ max(float(offset_mm) for offset_mm in hole_probe_offsets_mm)
		+ 30.0,
	)

	return ScanBounds(
		x_min=base_bounds.x_min,
		x_max=base_bounds.x_max,
		y_min=max(base_bounds.y_min, center_y_mm - local_margin_mm),
		y_max=min(base_bounds.y_max, center_y_mm + local_margin_mm),
	)


def validate_rescan_shelf_args(
	args: argparse.Namespace,
	rescan_target: Any,
	validation_target: Any,
) -> None:
	if args.rescan_shelf is None:
		return

	if args.rescan_shelf < 0:
		raise ValueError('--rescan-shelf must be non-negative')

	if args.from_run is not None:
		raise ValueError('--rescan-shelf cannot be used with --from-run')

	if args.continue_scan is True:
		raise ValueError('--rescan-shelf cannot be used with --continue')

	if rescan_target is not None:
		raise ValueError('--rescan-shelf cannot be used with --rescan-cell')

	if validation_target is not None:
		raise ValueError('--rescan-shelf cannot be used with --validate-cell')

	if args.known_shelf_edge_y is not None:
		raise ValueError('--rescan-shelf derives the known shelf edge; drop --known-shelf-edge-y')

	if args.x is not None or args.y is not None:
		raise ValueError('--rescan-shelf derives its bounds from the scan config; drop --x/--y')

	if args.y_offset != 0.0:
		raise ValueError('--rescan-shelf derives its bounds from the scan config; drop --y-offset')

	if args.module is None:
		raise ValueError('--module is required with --rescan-shelf')


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


def derive_scans_dir(
	config_path: str,
	runtime_paths: RuntimeStatePaths | None = None,
) -> str:
	paths = runtime_paths if runtime_paths is not None else paths_for_config_file(config_path)
	return str(paths.calibration_dir)


def paths_for_selected_config(config_path: str) -> RuntimeStatePaths:
	selected_config_path = Path(config_path).resolve()
	active_paths = RuntimeStatePaths.current()

	if selected_config_path == active_paths.config_path:
		return active_paths

	return replace(active_paths, config_path=selected_config_path)



def load_run_parameters_for_analysis(analysis: Any) -> dict[str, Any]:
	run_parameters_path = Path(analysis.metadata_path).resolve().parent / 'run_parameters.json'

	if run_parameters_path.exists() is False:
		return {}

	document = laser_module.load_json_file(run_parameters_path)
	return dict(document.get('run_parameters', {}))


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
	cli_rescan_shelf = run_parameters.get('cli', {}).get('rescan_shelf')

	if isinstance(auto_calibration_scope, dict) is True:
		scope_mode = auto_calibration_scope.get('mode')

		if scope_mode == 'shelf':
			laser_module.apply_single_shelf_analysis(
				analysis=analysis,
				grid=grid,
				grid_path=grid_path,
				scan_config=config.robot.grid.scan,
				side=analysis.side,
				target_shelf_idx=int(auto_calibration_scope['shelf_idx']),
			)
			return

	if cli_rescan_shelf is not None:
		laser_module.apply_single_shelf_analysis(
			analysis=analysis,
			grid=grid,
			grid_path=grid_path,
			scan_config=config.robot.grid.scan,
			side=analysis.side,
			target_shelf_idx=int(cli_rescan_shelf),
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

		laser_module.apply_single_cell_rescan(
			grid=grid,
			target=target,
			side=analysis.side,
			detected_cell=matched_cell,
			scan_config=config.robot.grid.scan,
		)
		return

	laser_module.apply_analysis(
		analysis=analysis,
		grid=grid,
		grid_path=grid_path,
		scan_config=config.robot.grid.scan,
		side=analysis.side,
	)


async def run_scan(args: argparse.Namespace) -> int:
	runtime_paths = paths_for_selected_config(args.config)
	ensure_mode_workspace(runtime_paths)
	laser_module.print_status('phase=config')
	config = load_config_file(args.config)
	scans_dir = derive_scans_dir(args.config, runtime_paths)
	args.scans_dir = scans_dir
	scan_config = apply_scan_config_overrides(config.robot.grid.scan, args)
	recorder: laser_module.StagedScanRecorder | None = None
	rescan_target = parse_single_cell_rescan_target(args.rescan_cell)
	validation_target = parse_single_cell_rescan_target(args.validate_cell)
	validate_rescan_shelf_args(args, rescan_target, validation_target)

	if args.coarse_scan_rpm is not None and args.coarse_scan_rpm <= 0:
		raise ValueError('--coarse-scan-rpm must be greater than zero')

	if args.fine_scan_feed_mm_s is not None and args.fine_scan_feed_mm_s <= 0:
		raise ValueError('--fine-scan-feed-mm-s must be greater than zero')
	rescan_candidate: laser_module.GapCandidateRegion | None = None
	grid_path = runtime_paths.resolve_storage_path(config, config_path=args.config)
	grid = laser_module.load_grid(grid_path)

	if args.from_run is not None:
		if args.continue_scan is True:
			raise ValueError('--from-run cannot be used with --continue')

		if args.npy is not None:
			raise ValueError('--from-run cannot be used with --npy')

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
		# The earlier portion of the run already moved with the recorded style/feed,
		# so the resumed portion restores them instead of the current config values.
		# Recordings that predate the style fields were necessarily stepped.
		resolved_scan_settings = run_parameters.get('resolved', {})
		scan_overrides = {
			'coarse_scan_style': laser_module.COARSE_SCAN_STYLE_STEPPED,
			'fine_scan_style': laser_module.COARSE_SCAN_STYLE_STEPPED,
		}
		scan_overrides.update({
			key: resolved_scan_settings[key]
			for key in (
				'coarse_scan_style',
				'coarse_scan_rpm',
				'coarse_scan_bin_mode',
				'fine_scan_style',
				'fine_scan_feed_mm_s',
			)
			if key in resolved_scan_settings
		})
		scan_config = scan_config.model_copy(update=scan_overrides)
		coarse_style = scan_overrides.get('coarse_scan_style')
		coarse_rpm = scan_overrides.get('coarse_scan_rpm')
		fine_style = scan_overrides.get('fine_scan_style')
		fine_feed_mm_s = scan_overrides.get('fine_scan_feed_mm_s')
		laser_module.print_status(
			f'resume_scan_style=coarse:{coarse_style} coarse_rpm:{coarse_rpm} '
			f'fine:{fine_style} fine_feed_mm_s:{fine_feed_mm_s}'
		)
		resumed_rescan_shelf = run_parameters.get('cli', {}).get('rescan_shelf')

		if resumed_rescan_shelf is not None:
			args.rescan_shelf = int(resumed_rescan_shelf)

			if known_shelf_edge_y is None and shelf_scout_only is False:
				known_shelf_edge_y = laser_module.build_single_shelf_known_edge_y(
					grid=grid,
					side=side,
					shelf_idx=int(args.rescan_shelf),
					scan_config=scan_config,
				)
			laser_module.print_status(
				f'resume_rescan_shelf={args.rescan_shelf} '
				f'known_shelf_edge_y={known_shelf_edge_y}'
			)
		laser_module.print_status(f'resume_run_id={recorder.run_id}')
	else:
		if args.module is None:
			raise ValueError('--module is required unless --continue is used')

		if args.step is None:
			raise ValueError('--step is required unless --continue is used')

		if rescan_target is None and args.rescan_shelf is None and args.x is None:
			raise ValueError('--x is required unless --continue, --rescan-cell or --rescan-shelf is used')

		if rescan_target is None and args.rescan_shelf is None and args.y is None:
			raise ValueError('--y is required unless --continue, --rescan-cell or --rescan-shelf is used')

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
				laser_module.get_laser_sample_settle_ms()
			)
		)
		samples_per_point = (
			args.samples_per_point
			if args.samples_per_point is not None
			else laser_module.get_fine_samples_per_point(
				laser_module.get_laser_samples_per_point()
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
			if args.rescan_shelf is not None:
				target_shelf = laser_module.resolve_target_shelf(
					grid=grid,
					side=side,
					shelf_idx=int(args.rescan_shelf),
				)
				center_y_mm = shelf_calibration_center_y(target_shelf)

				if center_y_mm is None:
					raise ValueError(
						f'--rescan-shelf {args.rescan_shelf} has no calibration points on {side.value}'
					)

				# Stored calibration points are gripper coordinates; the scan window is laser
				# geometry, so convert before centring the bounds on it.
				center_y_mm = laser_module.convert_stored_y_mm_to_laser_frame(
					stored_y_mm=center_y_mm,
					side=side,
					scan_config=scan_config,
				)

				bounds = build_single_shelf_rescan_bounds(
					base_bounds=build_side_scan_bounds(scan_config, side),
					center_y_mm=center_y_mm,
					scan_config=scan_config,
				)

				if shelf_scout_only is True:
					# A scout-only pass over the window is the quickest way to compare a flying
					# scout against a stepped one, so the known edge is not seeded for it.
					known_shelf_edge_y = None
				else:
					known_shelf_edge_y = laser_module.build_single_shelf_known_edge_y(
						grid=grid,
						side=side,
						shelf_idx=int(args.rescan_shelf),
						scan_config=scan_config,
					)

				laser_module.print_status(
					f'rescan_shelf={args.rescan_shelf} center_y={center_y_mm:.2f} '
					f'bounds:y=({bounds.y_min:.2f},{bounds.y_max:.2f}) '
					f'known_shelf_edge_y={known_shelf_edge_y}'
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
				scan_config=scan_config,
			)
		elif args.rescan_shelf is not None:
			laser_module.apply_single_shelf_analysis(
				analysis=analysis,
				grid=grid,
				grid_path=grid_path,
				scan_config=scan_config,
				side=side,
				target_shelf_idx=int(args.rescan_shelf),
			)
		else:
			laser_module.apply_analysis(
				analysis=analysis,
				grid=grid,
				grid_path=grid_path,
				scan_config=scan_config,
				side=side,
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
