from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from openshelf_os.config.loader import CONFIG_PATH
from openshelf_os.runtime_state import ensure_mode_workspace

from .reporting import write_stage_stall_output_document
from .stage_stall import run_stage_stall_capture
from .status import print_status


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


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description='Capture laser rotation calibration with stage-stall probing.'
	)
	subparsers = parser.add_subparsers(dest='command', required=True)

	stage_stall_parser = subparsers.add_parser('stage-stall')
	stage_stall_parser.add_argument('--module', choices=['front', 'back'], required=True)
	stage_stall_parser.add_argument('--y-range', nargs=2, metavar=('MIN', 'MAX'), type=float, required=True)
	stage_stall_parser.add_argument('--scout-x-mm', type=float)
	stage_stall_parser.add_argument('--probe-x-mm', type=float)
	stage_stall_parser.add_argument('--rotation-range', nargs=3, metavar=('MIN', 'MAX', 'STEP'), type=float, required=True)
	stage_stall_parser.add_argument('--config', type=str, default=str(CONFIG_PATH))
	stage_stall_parser.add_argument('--settle-ms', type=int)
	stage_stall_parser.add_argument('--samples-per-point', type=int)
	stage_stall_parser.add_argument('--stage-extension-mm', type=float)
	stage_stall_parser.add_argument('--base-z-mm', type=float)
	stage_stall_parser.add_argument('--rotation-center-deg', type=float)
	stage_stall_parser.add_argument('--shutdown-action', choices=['stop', 'shutdown'], default='shutdown')
	stage_stall_parser.add_argument('--stage-start-mm', type=float, default=3.0)
	stage_stall_parser.add_argument('--stage-target-mm', type=float)
	stage_stall_parser.add_argument('--stage-backoff-mm', type=float, default=8.0)
	stage_stall_parser.add_argument('--stage-probe-rpm', type=int)
	stage_stall_parser.add_argument('--fast-approach-mm', type=float)
	stage_stall_parser.add_argument('--fast-approach-margin-mm', type=float, default=2.0)
	stage_stall_parser.add_argument('--fast-approach-rpm', type=int)
	stage_stall_parser.add_argument('--probe-step-mm', type=float, default=0.5)
	stage_stall_parser.add_argument('--stage-timeout-s', type=float, default=30.0)
	stage_stall_parser.add_argument('--no-progress-timeout-s', type=float, default=0.25)
	stage_stall_parser.add_argument('--fall-threshold-mm', type=float, default=0.15)
	stage_stall_parser.add_argument('--stop-after-fall-count', type=int, default=1)
	stage_stall_parser.add_argument('--output', type=str)

	return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
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

		if args.fast_approach_mm is not None and args.fast_approach_mm < 0.0:
			raise ValueError('--fast-approach-mm must be non-negative')

		if args.fast_approach_rpm is not None and args.fast_approach_rpm <= 0:
			raise ValueError('--fast-approach-rpm must be positive')

		if args.probe_step_mm <= 0.0:
			raise ValueError('--probe-step-mm must be positive')

		if args.fall_threshold_mm < 0.0:
			raise ValueError('--fall-threshold-mm must be non-negative')

		if args.stop_after_fall_count <= 0:
			raise ValueError('--stop-after-fall-count must be greater than zero')

		return

	raise ValueError(f'unsupported command: {args.command}')


def main() -> int:
	ensure_mode_workspace()
	args = parse_args()
	validate_args(args)
	return asyncio.run(run_stage_stall_async(args))


if __name__ == '__main__':
	raise SystemExit(main())
