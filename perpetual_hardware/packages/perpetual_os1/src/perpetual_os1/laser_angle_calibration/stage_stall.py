from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import numpy as np

from .capture import enter_capture_mode, prepare_capture_pose, resolve_capture_y_mm, resolve_rotation_center_deg
from .config import (
	get_laser_sample_settle_ms,
	get_laser_samples_per_point,
	get_laser_scan_stage_extension_mm,
	get_laser_scan_z_offset_from_place_mm,
)
from .geometry import build_axis_positions
from .models import StageStallProbeSample
from .robot_ops import probe_stage_until_stall, run_with_temp_robot_config
from .shelf_scout import build_shelf_scout_x_positions
from .status import print_status


def resolve_probe_x_mm(
	*,
	args: argparse.Namespace,
	scan_config: Any,
	side_name: str,
) -> float:
	if args.probe_x_mm is not None:
		return float(args.probe_x_mm)

	if args.scout_x_mm is not None:
		return float(args.scout_x_mm)

	scout_x_positions = build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=0.0,
		x_max_mm=0.0,
		side_name=side_name,
		scout_x_mm=None,
	)
	return float(scout_x_positions[0])


def summarize_stage_stall_samples(
	samples: list[StageStallProbeSample],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
	rotation_buckets: dict[float, list[float]] = {}
	rotation_metadata: dict[float, dict[str, Any]] = {}

	for sample in samples:
		bucket = rotation_buckets.setdefault(sample.rotation_deg, [])

		if sample.stalled is True:
			bucket.append(sample.stage_position_mm)

		rotation_metadata[sample.rotation_deg] = {
			'rotation_deg': float(sample.rotation_deg),
			'absolute_rotation_deg': float(sample.absolute_rotation_deg),
		}

	angle_summaries: list[dict[str, Any]] = []
	best_summary: dict[str, Any] | None = None

	for rotation_deg in sorted(rotation_metadata):
		stage_positions = rotation_buckets.get(rotation_deg, [])
		metadata = rotation_metadata[rotation_deg]
		summary = {
			'rotation_deg': metadata['rotation_deg'],
			'absolute_rotation_deg': metadata['absolute_rotation_deg'],
			'stall_count': len(stage_positions),
			'median_stage_position_mm': (
				None if len(stage_positions) == 0 else float(np.median(np.array(stage_positions, dtype=np.float64)))
			),
			'min_stage_position_mm': None if len(stage_positions) == 0 else float(min(stage_positions)),
			'max_stage_position_mm': None if len(stage_positions) == 0 else float(max(stage_positions)),
		}
		angle_summaries.append(summary)

		if summary['median_stage_position_mm'] is None:
			continue

		if best_summary is None:
			best_summary = summary
			continue

		if float(summary['median_stage_position_mm']) > float(best_summary['median_stage_position_mm']):
			best_summary = summary

	return angle_summaries, best_summary


def should_stop_after_stage_drop(
	*,
	best_stage_position_mm: float | None,
	current_stage_position_mm: float | None,
	previous_stage_position_mm: float | None,
	fall_threshold_mm: float,
	fall_run_count: int,
	stop_after_fall_count: int,
) -> tuple[bool, int]:
	if best_stage_position_mm is None:
		return False, 0

	if current_stage_position_mm is None or previous_stage_position_mm is None:
		return False, 0

	if current_stage_position_mm >= best_stage_position_mm - fall_threshold_mm:
		return False, 0

	if current_stage_position_mm >= previous_stage_position_mm:
		return False, 0

	updated_run_count = fall_run_count + 1
	return updated_run_count >= stop_after_fall_count, updated_run_count


def resolve_stage_retract_target(
	*,
	did_stall: bool,
	stage_position_mm: float,
	probe_start_mm: float,
	stage_backoff_mm: float,
) -> float:
	if did_stall is False:
		return probe_start_mm

	return max(probe_start_mm, stage_position_mm - max(stage_backoff_mm, 0.0))


async def retract_stage_after_stall(
	tool: Any,
	*,
	retract_stage_mm: float,
	probe_rpm: int | None,
	settle_s: float = 0.15,
	max_attempts: int = 2,
) -> None:
	last_error: BaseException | None = None

	for attempt_idx in range(max_attempts):
		await tool._ms.clear_alarm()

		if settle_s > 0.0:
			await asyncio.sleep(settle_s)

		try:
			await tool._ms.move_to_position(retract_stage_mm, rpm=probe_rpm)
			return
		except Exception as exc:
			last_error = exc
			print_status(
				f'stage_probe=retract_retry attempt={attempt_idx + 1}/{max_attempts} '
				f'error={exc}'
			)

	if last_error is not None:
		raise last_error

	raise RuntimeError('stage retract after stall failed')


async def finish_stage_stall_capture(
	tool: Any,
	*,
	z_move_rpm: float,
	z_move_accel: float,
) -> None:
	await tool._mg.move_to_position(1, rpm=z_move_rpm, accel=z_move_accel)
	await tool._ms.idle()

async def run_stage_stall_capture(
	args: argparse.Namespace,
) -> tuple[list[StageStallProbeSample], dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
	from perpetual_os1.common import load_config_file

	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	settle_ms = int(
		args.settle_ms
		if args.settle_ms is not None
		else get_laser_sample_settle_ms(scan_config)
	)
	samples_per_point = int(
		args.samples_per_point
		if args.samples_per_point is not None
		else get_laser_samples_per_point(scan_config)
	)
	rotation_positions = build_axis_positions(
		args.rotation_range[0],
		args.rotation_range[1],
		args.rotation_range[2],
	)
	stage_extension_mm = float(
		args.stage_extension_mm
		if args.stage_extension_mm is not None
		else get_laser_scan_stage_extension_mm(scan_config)
	)
	laser_scan_z_offset_from_place_mm = get_laser_scan_z_offset_from_place_mm(
		scan_config
	)
	samples: list[StageStallProbeSample] = []

	async def did_run(
		robot: Any,
	) -> tuple[list[StageStallProbeSample], dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
		tool = robot._tool

		if tool._laser_enabled is False or tool._laser is None:
			raise ValueError('laser is not enabled')

		if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
			raise ValueError('x, y, z, stage, and rotation motors are required')

		side_name = args.module
		side_number = 0 if side_name == 'left' else 1
		rotation_center_deg = float(
			args.rotation_center_deg
			if args.rotation_center_deg is not None
			else resolve_rotation_center_deg(side_name, config.robot.tool.parameters)
		)

		for rotation_deg in rotation_positions:
			absolute_rotation_deg = rotation_center_deg + float(rotation_deg)

			rotation_limit_deg = float(tool._mr._params.lim)
			if absolute_rotation_deg < 0.0 or absolute_rotation_deg > rotation_limit_deg:
				raise ValueError(
					'rotation sweep exceeds motor range '
					f'(absolute target {absolute_rotation_deg:.3f} deg is outside 0..{rotation_limit_deg:.3f})'
				)

		z_extension_place = await tool.get_param('Z_EXTENSION_PLACE')
		z_move_rpm = await tool.get_param('Z_MOVE_RPM')
		z_move_accel = await tool.get_param('Z_MOVE_ACCEL')

		if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
			raise ValueError('Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required')

		base_z_mm = float(
			args.base_z_mm
			if args.base_z_mm is not None
			else float(z_extension_place) + laser_scan_z_offset_from_place_mm
		)
		probe_x_mm = resolve_probe_x_mm(args=args, scan_config=scan_config, side_name=side_name)
		capture_y_mm = await resolve_capture_y_mm(
			tool,
			args=args,
			scan_config=scan_config,
			side_name=side_name,
			side_number=side_number,
			x_positions=[probe_x_mm],
			rotation_center_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			base_z_mm=base_z_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
		)
		stage_limit_mm = float(tool._ms._params.lim)
		probe_target_mm = float(args.stage_target_mm if args.stage_target_mm is not None else stage_limit_mm)
		probe_start_mm = float(args.stage_start_mm)
		default_probe_rpm = min(int(tool._ms._params.rpm), 30)
		probe_rpm = int(args.stage_probe_rpm if args.stage_probe_rpm is not None else default_probe_rpm)
		fast_approach_rpm = int(args.fast_approach_rpm if args.fast_approach_rpm is not None else probe_rpm)
		probe_step_mm = float(args.probe_step_mm)
		stage_backoff_mm = float(args.stage_backoff_mm)
		fall_run_count = 0
		best_stage_position_mm: float | None = None
		previous_stage_position_mm: float | None = None

		def build_metadata() -> dict[str, Any]:
			return {
				'config_path': str(Path(args.config).resolve()),
				'module': side_name,
				'probe_x_mm': probe_x_mm,
				'y_mm': capture_y_mm,
				'y_range': None if args.y_range is None else [float(args.y_range[0]), float(args.y_range[1])],
				'rotation_positions': rotation_positions,
				'rotation_center_deg': rotation_center_deg,
				'stage_limit_mm': stage_limit_mm,
				'stage_start_mm': probe_start_mm,
				'stage_target_mm': probe_target_mm,
				'stage_probe_rpm': probe_rpm,
				'stage_backoff_mm': stage_backoff_mm,
				'fast_approach_mm': float(args.fast_approach_mm),
				'fast_approach_rpm': fast_approach_rpm,
				'probe_step_mm': probe_step_mm,
				'no_progress_timeout_s': float(args.no_progress_timeout_s),
				'base_z_mm': base_z_mm,
				'settle_ms': settle_ms,
				'samples_per_point': samples_per_point,
			}

		def didPersistSamples() -> None:
			if args.output is None:
				return

			from .reporting import write_stage_stall_output_document

			angle_summaries, best_summary = summarize_stage_stall_samples(samples)
			write_stage_stall_output_document(
				output_path=Path(args.output),
				metadata=build_metadata(),
				samples=samples,
				angle_summaries=angle_summaries,
				best_summary=best_summary,
			)

		await enter_capture_mode(
			tool,
			side_name=side_name,
			side_number=side_number,
			x_mm=probe_x_mm,
			y_mm=capture_y_mm,
			absolute_rotation_deg=rotation_center_deg + float(rotation_positions[0]),
			stage_extension_mm=probe_start_mm,
			z_mm=base_z_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
		)
		current_probe_start_mm = probe_start_mm

		for rotation_idx, rotation_deg in enumerate(rotation_positions, start=1):
			absolute_rotation_deg = rotation_center_deg + float(rotation_deg)
			print_status(
				f'phase=stage-stall rotation={rotation_idx}/{len(rotation_positions)} '
				f'absolute_rotation_deg={absolute_rotation_deg:.3f}'
			)
			await prepare_capture_pose(
				tool,
				side_name=side_name,
				side_number=side_number,
				x_mm=probe_x_mm,
				y_mm=capture_y_mm,
				absolute_rotation_deg=absolute_rotation_deg,
				stage_extension_mm=current_probe_start_mm,
				z_mm=base_z_mm,
				z_move_rpm=float(z_move_rpm),
				z_move_accel=float(z_move_accel),
				should_rotate=True,
				should_use_safe_right_rotation_path=True,
				should_move_z=False,
			)
			did_stall, stage_position_mm, status, alarm_code = await probe_stage_until_stall(
				tool,
				target_stage_mm=probe_target_mm,
				retract_stage_mm=current_probe_start_mm,
				settle_ms=settle_ms,
				timeout_s=float(args.stage_timeout_s),
				probe_rpm=probe_rpm,
				no_progress_s=float(args.no_progress_timeout_s),
				probe_step_mm=probe_step_mm,
				fast_approach_mm=float(args.fast_approach_mm),
				fast_approach_rpm=fast_approach_rpm,
				auto_retract=False,
			)
			samples.append(
				StageStallProbeSample(
					rotation_deg=float(rotation_deg),
					absolute_rotation_deg=absolute_rotation_deg,
					probe_x_mm=probe_x_mm,
					probe_y_mm=capture_y_mm,
					stage_position_mm=stage_position_mm,
					stalled=did_stall,
					status=status,
					alarm_code=None if alarm_code is None else int(alarm_code),
				)
			)
			didPersistSamples()

			should_stop = False

			if did_stall is True:
				print_status(
					f'stage_probe=stall rotation_deg={float(rotation_deg):.3f} '
					f'absolute_rotation_deg={absolute_rotation_deg:.3f} '
					f'stage_position_mm={stage_position_mm:.3f}'
				)

				if best_stage_position_mm is None or stage_position_mm > best_stage_position_mm:
					best_stage_position_mm = stage_position_mm

				should_stop, fall_run_count = should_stop_after_stage_drop(
					best_stage_position_mm=best_stage_position_mm,
					current_stage_position_mm=stage_position_mm,
					previous_stage_position_mm=previous_stage_position_mm,
					fall_threshold_mm=float(args.fall_threshold_mm),
					fall_run_count=fall_run_count,
					stop_after_fall_count=int(args.stop_after_fall_count),
				)
				previous_stage_position_mm = stage_position_mm
			else:
				print_status(
					f'stage_probe=skip rotation_deg={float(rotation_deg):.3f} '
					f'absolute_rotation_deg={absolute_rotation_deg:.3f} '
					f'status={status} stage_position_mm={stage_position_mm:.3f}'
				)
				fall_run_count = 0

			stage_retract_target_mm = resolve_stage_retract_target(
				did_stall=did_stall,
				stage_position_mm=stage_position_mm,
				probe_start_mm=probe_start_mm,
				stage_backoff_mm=stage_backoff_mm,
			)
			print_status(f'stage_probe=retract target_mm={stage_retract_target_mm:.3f}')
			await retract_stage_after_stall(
				tool,
				retract_stage_mm=stage_retract_target_mm,
				probe_rpm=probe_rpm,
			)
			current_probe_start_mm = stage_retract_target_mm

			if should_stop is True:
				print_status('stage_probe=stop reason=stage_position_falling')
				break
		await finish_stage_stall_capture(
			tool,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
		)
		angle_summaries, best_summary = summarize_stage_stall_samples(samples)

		return samples, build_metadata(), angle_summaries, best_summary

	return await run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		did_run=did_run,
		shutdown_action=args.shutdown_action,
	)
