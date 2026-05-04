from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .capture import capture_samples_with_robot
from .geometry import build_axis_positions, generate_synthetic_samples, search_best_alpha
from .reporting import (
	load_samples,
	print_estimated_rotation_from_metadata,
	print_fit_result,
	write_output_document,
	write_stage_stall_output_document,
)
from .stage_stall import run_stage_stall_capture
from .status import print_status


def run_fit(args: argparse.Namespace) -> int:
	input_path = Path(args.input)
	input_document = json.loads(input_path.read_text(encoding='utf-8'))
	input_metadata = input_document.get('metadata', {}) if isinstance(input_document, dict) is True else {}
	samples = load_samples(input_path)
	fit_result = search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
	)
	print_fit_result(fit_result)
	print_estimated_rotation_from_metadata(fit_result=fit_result, metadata=input_metadata)

	if args.output is not None:
		write_output_document(
			output_path=Path(args.output),
			mode='fit',
			samples=samples,
			fit_result=fit_result,
			metadata={
				**input_metadata,
				'input': str(input_path.resolve()),
				'laser_offset_x_mm': float(args.laser_offset_x_mm),
				'laser_offset_z_mm': float(args.laser_offset_z_mm),
				'beam_zero_deg': float(args.beam_zero_deg),
			},
		)

	return 0


def run_simulate(args: argparse.Namespace) -> int:
	axis_x_positions = build_axis_positions(
		args.axis_x_range[0],
		args.axis_x_range[1],
		args.axis_x_range[2],
	)
	axis_z_positions = build_axis_positions(
		args.axis_z_range[0],
		args.axis_z_range[1],
		args.axis_z_range[2],
	)
	rotation_positions = build_axis_positions(
		args.rotation_range[0],
		args.rotation_range[1],
		args.rotation_range[2],
	)
	samples = generate_synthetic_samples(
		axis_x_positions=axis_x_positions,
		axis_z_positions=axis_z_positions,
		rotation_positions=rotation_positions,
		true_alpha_deg=float(args.true_alpha_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
		line_point_x_mm=float(args.line_point_x_mm),
		line_point_z_mm=float(args.line_point_z_mm),
		line_direction_deg=float(args.line_direction_deg),
		noise_std_mm=float(args.noise_std_mm),
		seed=int(args.seed),
	)
	fit_result = search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
	)
	alpha_error_deg = abs(float(args.true_alpha_deg) - fit_result.alpha_deg)
	print_status(f'true_alpha_deg={float(args.true_alpha_deg):.4f}')
	print_fit_result(fit_result)
	print_status(f'alpha_error_deg={alpha_error_deg:.6f}')

	if args.output is not None:
		write_output_document(
			output_path=Path(args.output),
			mode='simulate',
			samples=samples,
			fit_result=fit_result,
			metadata={
				'true_alpha_deg': float(args.true_alpha_deg),
				'noise_std_mm': float(args.noise_std_mm),
				'seed': int(args.seed),
				'line_point_x_mm': float(args.line_point_x_mm),
				'line_point_z_mm': float(args.line_point_z_mm),
				'line_direction_deg': float(args.line_direction_deg),
			},
		)

	if args.assert_max_alpha_error_deg is not None:
		if alpha_error_deg > float(args.assert_max_alpha_error_deg):
			print_status(
				f'alpha_error_check=failed max={float(args.assert_max_alpha_error_deg):.6f}'
			)
			return 1

	return 0


async def run_capture_async(args: argparse.Namespace) -> int:
	samples, capture_metadata = await capture_samples_with_robot(args)
	fit_result = search_best_alpha(
		samples,
		alpha_min_deg=float(args.alpha_min_deg),
		alpha_max_deg=float(args.alpha_max_deg),
		coarse_step_deg=float(args.coarse_step_deg),
		fine_window_deg=float(args.fine_window_deg),
		fine_step_deg=float(args.fine_step_deg),
		laser_offset_x_mm=float(args.laser_offset_x_mm),
		laser_offset_z_mm=float(args.laser_offset_z_mm),
		beam_zero_deg=float(args.beam_zero_deg),
	)
	print_fit_result(fit_result)
	print_estimated_rotation_from_metadata(fit_result=fit_result, metadata=capture_metadata)

	if args.output is not None:
		write_output_document(
			output_path=Path(args.output),
			mode='capture',
			samples=samples,
			fit_result=fit_result,
			metadata={
				**capture_metadata,
				'laser_offset_x_mm': float(args.laser_offset_x_mm),
				'laser_offset_z_mm': float(args.laser_offset_z_mm),
				'beam_zero_deg': float(args.beam_zero_deg),
			},
		)

	return 0


async def run_stage_stall_async(args: argparse.Namespace) -> int:
	samples, metadata, angle_summaries, best_summary = await run_stage_stall_capture(args)

	if best_summary is None:
		print_status('best_rotation_deg=none')
	else:
		print_status(f"best_rotation_deg={float(best_summary['rotation_deg']):.4f}")
		print_status(f"best_absolute_rotation_deg={float(best_summary['absolute_rotation_deg']):.4f}")
		print_status(f"best_stage_position_mm={float(best_summary['median_stage_position_mm']):.4f}")

	print_status(f'sample_count={len(samples)}')

	if args.output is not None:
		write_stage_stall_output_document(
			output_path=Path(args.output),
			metadata=metadata,
			samples=samples,
			angle_summaries=angle_summaries,
			best_summary=best_summary,
		)

	return 0


def parse_range_argument(value: list[float]) -> tuple[float, float, float]:
	if len(value) != 3:
		raise ValueError('range arguments require MIN MAX STEP')

	return float(value[0]), float(value[1]), float(value[2])


def parse_pair_argument(value: list[float]) -> tuple[float, float]:
	if len(value) != 2:
		raise ValueError('pair arguments require MIN MAX')

	return float(value[0]), float(value[1])


def add_fit_arguments(parser: argparse.ArgumentParser) -> None:
	parser.add_argument('--laser-offset-x-mm', type=float, default=0.0)
	parser.add_argument('--laser-offset-z-mm', type=float, default=0.0)
	parser.add_argument('--beam-zero-deg', type=float, default=0.0)
	parser.add_argument('--alpha-min-deg', type=float, default=-10.0)
	parser.add_argument('--alpha-max-deg', type=float, default=10.0)
	parser.add_argument('--coarse-step-deg', type=float, default=0.1)
	parser.add_argument('--fine-window-deg', type=float, default=0.3)
	parser.add_argument('--fine-step-deg', type=float, default=0.005)
	parser.add_argument('--output', type=str)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description='Capture or fit laser rotation alpha using flat-surface line residuals.'
	)
	subparsers = parser.add_subparsers(dest='command', required=True)

	fit_parser = subparsers.add_parser('fit')
	fit_parser.add_argument('--input', required=True, type=str)
	add_fit_arguments(fit_parser)

	simulate_parser = subparsers.add_parser('simulate')
	simulate_parser.add_argument('--true-alpha-deg', type=float, default=4.0)
	simulate_parser.add_argument('--noise-std-mm', type=float, default=0.05)
	simulate_parser.add_argument('--seed', type=int, default=0)
	simulate_parser.add_argument('--line-point-x-mm', type=float, default=160.0)
	simulate_parser.add_argument('--line-point-z-mm', type=float, default=0.0)
	simulate_parser.add_argument('--line-direction-deg', type=float, default=90.0)
	simulate_parser.add_argument('--axis-x-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, default=[0.0, 40.0, 10.0])
	simulate_parser.add_argument('--axis-z-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, default=[-8.0, 8.0, 4.0])
	simulate_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, default=[-8.0, 8.0, 1.0])
	simulate_parser.add_argument('--assert-max-alpha-error-deg', type=float)
	add_fit_arguments(simulate_parser)

	capture_parser = subparsers.add_parser('capture')
	capture_parser.add_argument('--module', choices=['left', 'right'], required=True)
	capture_parser.add_argument('--y-mm', type=float)
	capture_parser.add_argument('--y-range', nargs=2, metavar=('MIN', 'MAX'), type=float)
	capture_parser.add_argument('--scout-x-mm', type=float)
	capture_parser.add_argument('--x-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	capture_parser.add_argument('--z-offset-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	capture_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	capture_parser.add_argument('--config', type=str, default='data/config.json')
	capture_parser.add_argument('--settle-ms', type=int)
	capture_parser.add_argument('--samples-per-point', type=int)
	capture_parser.add_argument('--stage-extension-mm', type=float)
	capture_parser.add_argument('--base-z-mm', type=float)
	capture_parser.add_argument('--rotation-center-deg', type=float)
	capture_parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	add_fit_arguments(capture_parser)

	stage_stall_parser = subparsers.add_parser('stage-stall')
	stage_stall_parser.add_argument('--module', choices=['left', 'right'], required=True)
	stage_stall_parser.add_argument('--y-range', nargs=2, metavar=('MIN', 'MAX'), type=float, required=True)
	stage_stall_parser.add_argument('--scout-x-mm', type=float)
	stage_stall_parser.add_argument('--probe-x-mm', type=float)
	stage_stall_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	stage_stall_parser.add_argument('--config', type=str, default='data/config.json')
	stage_stall_parser.add_argument('--settle-ms', type=int)
	stage_stall_parser.add_argument('--samples-per-point', type=int)
	stage_stall_parser.add_argument('--stage-extension-mm', type=float)
	stage_stall_parser.add_argument('--base-z-mm', type=float)
	stage_stall_parser.add_argument('--rotation-center-deg', type=float)
	stage_stall_parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	stage_stall_parser.add_argument('--stage-start-mm', type=float, default=3.0)
	stage_stall_parser.add_argument('--stage-target-mm', type=float)
	stage_stall_parser.add_argument('--stage-probe-rpm', type=int)
	stage_stall_parser.add_argument('--fast-approach-mm', type=float, default=40.0)
	stage_stall_parser.add_argument('--fast-approach-rpm', type=int)
	stage_stall_parser.add_argument('--probe-step-mm', type=float, default=0.5)
	stage_stall_parser.add_argument('--stage-timeout-s', type=float, default=15.0)
	stage_stall_parser.add_argument('--no-progress-timeout-s', type=float, default=0.2)
	stage_stall_parser.add_argument('--fall-threshold-mm', type=float, default=0.15)
	stage_stall_parser.add_argument('--stop-after-fall-count', type=int, default=1)
	stage_stall_parser.add_argument('--output', type=str)

	return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
	if args.command == 'simulate':
		args.axis_x_range = parse_range_argument(args.axis_x_range)
		args.axis_z_range = parse_range_argument(args.axis_z_range)
		args.rotation_range = parse_range_argument(args.rotation_range)

	if args.command == 'capture':
		if args.y_range is not None:
			args.y_range = parse_pair_argument(args.y_range)

		args.x_range = parse_range_argument(args.x_range)
		args.z_offset_range = parse_range_argument(args.z_offset_range)
		args.rotation_range = parse_range_argument(args.rotation_range)

		if args.y_mm is None and args.y_range is None:
			raise ValueError('either --y-mm or --y-range is required for capture')

	if args.command == 'stage-stall':
		args.y_range = parse_pair_argument(args.y_range)
		args.rotation_range = parse_range_argument(args.rotation_range)

		if args.stage_start_mm < 0.0:
			raise ValueError('--stage-start-mm must be non-negative')

		if args.stage_target_mm is not None and args.stage_target_mm <= 0.0:
			raise ValueError('--stage-target-mm must be positive')

		if args.stage_timeout_s <= 0.0:
			raise ValueError('--stage-timeout-s must be positive')

		if args.no_progress_timeout_s <= 0.0:
			raise ValueError('--no-progress-timeout-s must be positive')

		if args.stage_probe_rpm is not None and args.stage_probe_rpm <= 0:
			raise ValueError('--stage-probe-rpm must be positive')

		if args.fast_approach_mm < 0.0:
			raise ValueError('--fast-approach-mm must be non-negative')

		if args.fast_approach_rpm is not None and args.fast_approach_rpm <= 0:
			raise ValueError('--fast-approach-rpm must be positive')

		if args.probe_step_mm <= 0.0:
			raise ValueError('--probe-step-mm must be positive')

		if args.fall_threshold_mm < 0.0:
			raise ValueError('--fall-threshold-mm must be non-negative')

		if args.stop_after_fall_count <= 0:
			raise ValueError('--stop-after-fall-count must be greater than zero')

	if args.command in {'fit', 'simulate', 'capture'}:
		if args.alpha_max_deg < args.alpha_min_deg:
			raise ValueError('--alpha-min-deg must be <= --alpha-max-deg')

		if args.coarse_step_deg <= 0.0:
			raise ValueError('--coarse-step-deg must be positive')

		if args.fine_step_deg <= 0.0:
			raise ValueError('--fine-step-deg must be positive')

		if args.fine_window_deg <= 0.0:
			raise ValueError('--fine-window-deg must be positive')


def main() -> int:
	args = parse_args()
	validate_args(args)

	if args.command == 'fit':
		return run_fit(args)

	if args.command == 'simulate':
		return run_simulate(args)

	if args.command == 'capture':
		return asyncio.run(run_capture_async(args))

	if args.command == 'stage-stall':
		return asyncio.run(run_stage_stall_async(args))

	raise ValueError(f'unsupported command: {args.command}')
