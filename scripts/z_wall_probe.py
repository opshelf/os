#!/usr/bin/env python3
"""One-shot Z/gripper wall-stall measurement for reference-less Z calibration.

The Z/gripper axis is mounted on the stage and cannot reach a tray wall on its own: the
stage first extends (~45-47mm on the left) to bring the gripper in, then Z extends a long
way (up to its ~180mm range) until the tip bumps the wall. This positions at a single
(x, y), rotates to the configured side angle, extends the stage to the grab extension,
reads the laser wall depth D, then GENTLY creeps Z toward the wall until it stalls on
contact (`probe_z_until_stall`) and reports the contact Z.

Because the laser and the gripper both ride the stage, the stage extension cancels in
`Z_contact - D`, which isolates the fixed laser->gripper-tip offset. Run it left and right;
the per-side difference should match the ~0.9mm we measured in K.

Run it supervised, one cell at a time, watching the machine.

SAFETY
  * Run with the long-running `api` container STOPPED (it holds the serial ports).
  * Rotation MUST be at the configured side angle (ROTATION_FRONT/BACK) before Z extends,
    or the gripper drives into the wrong place. This script sets it; verify on the camera.
  * The stage must be extended enough that Z can reach the wall within its range. The
    default stage extension is the side's STAGE_EXTENSION_SHELF_* (the grab value).
  * `--z-target-mm` is a HARD CAP. The wall is near full Z extension, so set this near the
    Z motor limit but below it. If the probe never stalls, it stops at the cap and retracts.
  * The probe creeps slowly (small steps, low rpm) and stops + retracts the instant the
    motor alarms or stops making progress. By default there is no fast-approach: it creeps
    the whole way from `--z-start-mm`. Use `--z-fast-approach-mm` to skip the known-clear
    portion (e.g. up to ~150mm) and only creep the final approach.
  * Increasing Z extends toward the wall (retract is the low end). Confirm before running.

Example (on device, after stopping api and bind-mounting scripts/):
  uv run python /app/scripts/z_wall_probe.py --module left --x 278 --y 44 \
    --z-start-mm 5 --z-fast-approach-mm 150 --z-target-mm 180 \
    --config /app/openshelf_hardware/data/config.json
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from openshelf_os.common import load_config_file
from openshelf_os.laser_calibration.angle.capture import (
	enter_capture_mode,
	resolve_rotation_center_deg,
)
from openshelf_os.laser_calibration.angle.config import (
	get_laser_sample_settle_ms,
	get_laser_samples_per_point,
)
from openshelf_os.laser_calibration.angle.robot_ops import (
	probe_z_until_stall,
	run_with_temp_robot_config,
	sample_laser_depth,
)
from openshelf_os.laser_calibration.angle.status import print_status

CONFIG_PATH = Path(__file__).resolve().parent.parent / "openshelf_hardware" / "data" / "config.json"


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description="Single-cell Z/gripper wall-stall measurement.")
	parser.add_argument("--module", choices=["left", "right"], required=True)
	parser.add_argument("--x", type=float, required=True, help="probe X (mm)")
	parser.add_argument("--y", type=float, required=True, help="probe Y (mm)")
	parser.add_argument("--z-start-mm", type=float, required=True, help="retracted Z to start from / return to")
	parser.add_argument("--z-target-mm", type=float, required=True, help="HARD CAP: max Z; stop here if no stall")
	parser.add_argument("--z-fast-approach-mm", type=float, default=None, help="creep from here; default = z-start (no fast approach)")
	parser.add_argument("--z-probe-rpm", type=int, default=None, help="probe rpm (default min(motor rpm, 20))")
	parser.add_argument("--z-fast-approach-rpm", type=int, default=None)
	parser.add_argument("--z-probe-step-mm", type=float, default=0.5)
	parser.add_argument("--z-no-progress-s", type=float, default=0.25)
	parser.add_argument("--z-timeout-s", type=float, default=60.0)
	parser.add_argument("--stage-extension-mm", type=float, default=None)
	parser.add_argument("--rotation-center-deg", type=float, default=None)
	parser.add_argument("--settle-ms", type=int, default=None)
	parser.add_argument("--samples-per-point", type=int, default=None)
	parser.add_argument("--config", default=str(CONFIG_PATH))
	parser.add_argument("--shutdown-action", default="stop")
	return parser


async def run(args: argparse.Namespace) -> int:
	config = load_config_file(args.config)
	scan_config = config.robot.grid.scan
	settle_ms = int(args.settle_ms if args.settle_ms is not None else get_laser_sample_settle_ms(scan_config))
	samples_per_point = int(
		args.samples_per_point if args.samples_per_point is not None else get_laser_samples_per_point(scan_config)
	)

	async def did_run(robot: object) -> int:
		tool = robot._tool

		if tool._laser_enabled is False or tool._laser is None:
			raise ValueError("laser is not enabled")

		if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
			raise ValueError("x, y, z, stage, and rotation motors are required")

		side_name = args.module
		side_number = 0 if side_name == "left" else 1
		rotation_center_deg = float(
			args.rotation_center_deg
			if args.rotation_center_deg is not None
			else resolve_rotation_center_deg(side_name, config.robot.tool.parameters)
		)

		z_limit_mm = float(tool._mg._params.lim)
		z_target_mm = float(args.z_target_mm)
		z_start_mm = float(args.z_start_mm)

		if z_target_mm > z_limit_mm:
			raise ValueError(f"--z-target-mm {z_target_mm:.3f} exceeds Z motor limit {z_limit_mm:.3f}")

		if z_start_mm >= z_target_mm:
			raise ValueError(f"--z-start-mm {z_start_mm:.3f} must be less than --z-target-mm {z_target_mm:.3f}")

		z_move_rpm = await tool.get_param("Z_MOVE_RPM")
		z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

		if z_move_rpm is None or z_move_accel is None:
			raise ValueError("Z_MOVE_RPM and Z_MOVE_ACCEL are required")

		stage_param = "STAGE_EXTENSION_SHELF_FRONT" if side_name == "left" else "STAGE_EXTENSION_SHELF_BACK"
		default_stage_extension = await tool.get_param(stage_param)

		if args.stage_extension_mm is None and default_stage_extension is None:
			raise ValueError(f"{stage_param} is required (or pass --stage-extension-mm)")

		stage_extension_mm = float(
			args.stage_extension_mm if args.stage_extension_mm is not None else default_stage_extension
		)

		default_probe_rpm = min(int(tool._mg._params.rpm), 20)
		probe_rpm = int(args.z_probe_rpm if args.z_probe_rpm is not None else default_probe_rpm)
		fast_approach_rpm = int(args.z_fast_approach_rpm if args.z_fast_approach_rpm is not None else probe_rpm)
		fast_approach_mm = float(args.z_fast_approach_mm if args.z_fast_approach_mm is not None else z_start_mm)

		print_status(
			f"phase=z-wall-probe module={side_name} x={args.x:.2f} y={args.y:.2f} "
			f"rotation_deg={rotation_center_deg:.3f} stage_extension_mm={stage_extension_mm:.2f} "
			f"z_start={z_start_mm:.2f} z_target={z_target_mm:.2f} probe_rpm={probe_rpm} step={args.z_probe_step_mm}"
		)

		await enter_capture_mode(
			tool,
			side_name=side_name,
			side_number=side_number,
			x_mm=float(args.x),
			y_mm=float(args.y),
			absolute_rotation_deg=rotation_center_deg,
			stage_extension_mm=stage_extension_mm,
			z_mm=z_start_mm,
			z_move_rpm=float(z_move_rpm),
			z_move_accel=float(z_move_accel),
		)

		wall_depth_mm = await sample_laser_depth(tool, settle_ms=settle_ms, samples_per_point=samples_per_point)
		print_status(f"laser_wall_depth_mm={'none' if wall_depth_mm is None else f'{wall_depth_mm:.3f}'}")

		did_stall, z_contact_mm, reason, alarm_code = await probe_z_until_stall(
			tool,
			target_z_mm=z_target_mm,
			retract_z_mm=z_start_mm,
			settle_ms=settle_ms,
			timeout_s=float(args.z_timeout_s),
			probe_rpm=probe_rpm,
			no_progress_s=float(args.z_no_progress_s),
			probe_step_mm=float(args.z_probe_step_mm),
			fast_approach_mm=fast_approach_mm,
			fast_approach_rpm=fast_approach_rpm,
			auto_retract=True,
		)

		offset_mm = None if wall_depth_mm is None else (z_contact_mm - float(wall_depth_mm))
		print_status(
			f"z_wall_probe=result module={side_name} stalled={did_stall} reason={reason} "
			f"alarm={alarm_code} z_contact_mm={z_contact_mm:.3f} "
			f"wall_depth_mm={'none' if wall_depth_mm is None else f'{wall_depth_mm:.3f}'} "
			f"laser_to_tip_offset_mm={'none' if offset_mm is None else f'{offset_mm:.3f}'}"
		)

		await tool._mg.move_to_position(z_start_mm, rpm=probe_rpm)
		await tool._ms.idle()

		return 0 if did_stall else 2

	return await run_with_temp_robot_config(
		config=config,
		config_path=args.config,
		did_run=did_run,
		shutdown_action=args.shutdown_action,
	)


def main() -> None:
	args = build_parser().parse_args()
	raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
	main()
