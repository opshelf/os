#!/usr/bin/env python3
"""Supervised diagnostic for the lb4a Y home-zero offset.

Reads commanded vs actual position, following error and alarm state at each
step of the production Motor.home() sequence, to show where a post-home
offset (one revolution, 40 mm) appears.

  inspect  read-only snapshots; run after the app has homed and been stopped
  home     runs Motor.configure() + Motor.home() with checkpoint reads
  move     absolute Y move within 0-150 mm, sampled from the motor's own wait
           loop, with cruise speed against commanded speed

Checkpoint reads add ~100 ms of bus traffic between homing steps. Move samples
add none.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import y_brake_timing_diagnostic as base  # noqa: E402
from hardware.motor import Motor  # noqa: E402

HOME_CONFIRMATION = "SUPERVISED-Y-HOME-ZERO-DIAGNOSTIC"
HOME_TIMEOUT_S = 120.0
MOVE_CONFIRMATION = "SUPERVISED-Y-SPEED-DIAGNOSTIC-MOVE"
MOVE_MIN_MM = 0.0
MOVE_MAX_MM = 150.0
MOVE_MIN_DISTANCE_MM = 1.0
MOVE_TIMEOUT_S = 30.0
HOMED_STATUS_BIT = 6

# (address, word count). Two-word entries are signed 32-bit, high word first.
REGISTERS = {
    "status": (0x1003, 1),
    "alarm": (0x2203, 1),
    "trigger": (0x6002, 1),
    "software_enable": (0x000F, 1),
    "motor_direction": (0x0007, 1),
    "following_error_limit": (0x000B, 1),
    "alarm_detection": (0x016D, 1),
    "peak_current_0p1a": (0x0191, 1),
    "bus_voltage_0p1v": (0x0177, 1),
    "pr0_velocity_rpm": (0x6203, 1),
    "pr0_acceleration": (0x6204, 1),
    "pr0_deceleration": (0x6205, 1),
    "following_error": (0x1010, 2),
    "status_profile_position": (0x1012, 2),
    "status_feedback_position": (0x1014, 2),
    "pr_profile_position": (0x602A, 2),
    "pr_actual_position": (0x602C, 2),
}
POSITION_KEYS = (
    "status_profile_position",
    "status_feedback_position",
    "pr_profile_position",
    "pr_actual_position",
)

# Wrapped Motor steps: snapshot before and/or after each call.
BEFORE = ("_set_zero_position", "_clear_homing_trip")
AFTER = (
    "_arm_following_error_trip",
    "_start_path_zero",
    "_clear_homing_trip",
    "_set_zero_position",
    "move_config",
    "_park",
)


async def snapshot(controller: Any, params: Any) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, (address, count) in REGISTERS.items():
        words = await controller.read_holding_register_block(base.Y_AXIS_UNIT, address, count=count)
        values[name] = base.signed_position(words) if count == 2 else int(words[0])

    mm_per_raw = float(params.mpr) / int(params.ppr)
    for key in POSITION_KEYS:
        values[f"{key}_mm"] = round(values[key] * mm_per_raw, 3)
    values["pr_actual_minus_profile"] = values["pr_actual_position"] - values["pr_profile_position"]
    values["status_feedback_minus_profile"] = (
        values["status_feedback_position"] - values["status_profile_position"]
    )
    return values


def instrument(motor: Motor, controller: Any, params: Any, t0: float) -> None:
    counts: dict[str, int] = {}

    async def checkpoint(label: str) -> None:
        base.emit("checkpoint", label=label, t_s=round(time.monotonic() - t0, 3),
                  **await snapshot(controller, params))

    def wrap_async(name: str) -> None:
        original = getattr(motor, name, None)
        if original is None:
            return

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            counts[name] = counts.get(name, 0) + 1
            label = f"{name}#{counts[name]}"
            if name in BEFORE:
                await checkpoint(f"before:{label}")
            result = await original(*args, **kwargs)
            if name in AFTER:
                await checkpoint(f"after:{label}")
            return result

        setattr(motor, name, wrapper)

    for name in dict.fromkeys(BEFORE + AFTER):
        wrap_async(name)

    wait_for_move = motor.wait_for_move

    async def wait_wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await wait_for_move(*args, **kwargs)
        if kwargs.get("homing") is True:
            await checkpoint(f"after:homing_wait result={getattr(result, 'name', result)}")
        return result

    motor.wait_for_move = wait_wrapper

    is_limit_stop = motor._is_homing_limit_stop

    def limit_stop_wrapper(homing: bool, alarm_state: Any, state: Any) -> bool:
        tripped = is_limit_stop(homing, alarm_state, state)
        if tripped:
            now = time.monotonic()
            base.emit(
                "homing_trip",
                alarm_code=alarm_state.alarm_code,
                stall_to_trip_s=round(now - state.last_movement_time, 3),
                run_to_trip_s=round(now - state.start_time, 3),
            )
        return tripped

    motor._is_homing_limit_stop = limit_stop_wrapper


async def run_inspect(args: argparse.Namespace, controller: Any, params: Any) -> int:
    for i in range(args.samples):
        base.emit("inspect", sample=i, **await snapshot(controller, params))
        await asyncio.sleep(args.interval_ms / 1000)
    return 0


async def run_home(args: argparse.Namespace, controller: Any, params: Any) -> int:
    if args.confirm != HOME_CONFIRMATION:
        raise RuntimeError(f"--confirm must be {HOME_CONFIRMATION}")

    home_kwargs: dict[str, Any] = {}
    if args.startup:
        from openshelf_os.tool import Y_STARTUP_HOME_CLEARANCE_MM, Y_STARTUP_HOME_PEAK_CURRENT

        home_kwargs = {
            "pre_home_clearance_mm": Y_STARTUP_HOME_CLEARANCE_MM,
            "skip_pre_home_approach": True,
            "homing_peak_current": Y_STARTUP_HOME_PEAK_CURRENT,
        }

    base.emit("home_preflight", home_kwargs=home_kwargs, **await snapshot(controller, params))
    await base.arm_countdown("home", args.arm_delay_s)

    motor = Motor(controller, params)
    t0 = time.monotonic()
    instrument(motor, controller, params, t0)

    try:
        await motor.configure()
        async with asyncio.timeout(HOME_TIMEOUT_S):
            await motor.home(**home_kwargs)
    except BaseException as exc:
        await base.stop_after_motion_error(motor, exc)
        base.emit("home_failed", error=str(exc), **await snapshot(controller, params))
        raise

    for i in range(args.samples):
        base.emit("post_home", sample=i, t_s=round(time.monotonic() - t0, 3),
                  host_position_mm=await motor.get_position(), **await snapshot(controller, params))
        await asyncio.sleep(args.interval_ms / 1000)
    return 0


def cruise_speed_mm_s(samples: list[tuple[float, float]], start_mm: float, target_mm: float) -> float | None:
    """Mean speed across 20-80 % of the commanded travel."""
    span = target_mm - start_mm
    points = [(t, p) for t, p in samples if 0.2 <= (p - start_mm) / span <= 0.8]
    if len(points) < 2:
        return None
    (t0, p0), (t1, p1) = points[0], points[-1]
    return round(abs(p1 - p0) / (t1 - t0), 1)


async def run_move(args: argparse.Namespace, controller: Any, params: Any) -> int:
    if args.confirm != MOVE_CONFIRMATION:
        raise RuntimeError(f"--confirm must be {MOVE_CONFIRMATION}")
    if not MOVE_MIN_MM <= args.target_mm <= MOVE_MAX_MM:
        raise RuntimeError(f"--target-mm must be within {MOVE_MIN_MM:g}-{MOVE_MAX_MM:g} mm")
    rpm = args.rpm or int(params.rpm)
    if not 0 < rpm <= int(params.rpm):
        raise RuntimeError(f"--rpm must be within 1-{params.rpm}")

    before = await snapshot(controller, params)
    if not before["status"] & (1 << HOMED_STATUS_BIT):
        raise RuntimeError("drive does not report homing complete; home Y first")
    if before["alarm"] != 0:
        raise RuntimeError(f"drive alarm 0x{before['alarm']:04X} is active")

    motor = Motor(controller, params)
    start_mm = await motor.get_position()
    if abs(args.target_mm - start_mm) < MOVE_MIN_DISTANCE_MM:
        raise RuntimeError(f"target is within {MOVE_MIN_DISTANCE_MM:g} mm of {start_mm:.3f} mm")

    commanded_mm_s = round(rpm * float(params.mpr) / 60, 1)
    base.emit("move_preflight", start_mm=start_mm, target_mm=args.target_mm, rpm=rpm,
              commanded_mm_s=commanded_mm_s, **before)
    await base.arm_countdown("move", args.arm_delay_s)

    samples: list[tuple[float, float]] = []
    record = motor._record_flight_snapshot

    def record_wrapper(*record_args: Any) -> None:
        record(*record_args)
        entry = motor._flight_recorder[-1]
        if entry.logical_position_mm is not None:
            samples.append((entry.elapsed_s, entry.logical_position_mm))
        base.emit("move_sample", elapsed_s=round(entry.elapsed_s, 3), position_mm=entry.logical_position_mm,
                  status=entry.status_word, alarm=entry.alarm_code)

    motor._record_flight_snapshot = record_wrapper

    def summary() -> dict[str, Any]:
        cruise = cruise_speed_mm_s(samples, start_mm, args.target_mm)
        return {
            "commanded_mm_s": commanded_mm_s,
            "cruise_mm_s": cruise,
            "speed_ratio": None if cruise is None else round(cruise / commanded_mm_s, 2),
        }

    try:
        await motor.move_config()
        async with asyncio.timeout(MOVE_TIMEOUT_S):
            await motor.move_to_position(args.target_mm, rpm=rpm)
    except BaseException as exc:
        await base.stop_after_motion_error(motor, exc)
        base.emit("move_failed", error=str(exc), **summary(), **await snapshot(controller, params))
        raise

    base.emit("move_complete", **summary())
    for i in range(args.samples):
        base.emit("post_move", sample=i, host_position_mm=await motor.get_position(),
                  **await snapshot(controller, params))
        await asyncio.sleep(args.interval_ms / 1000)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=base.DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
    parser.add_argument("--timeout", type=float, default=2.0)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("inspect", "home", "move"):
        cmd = sub.add_parser(name)
        base.add_common_command_arguments(cmd)
        cmd.add_argument("--samples", type=int, default=10)
        cmd.add_argument("--interval-ms", type=float, default=100.0)

    home = sub.choices["home"]
    home.add_argument("--confirm", required=True)
    home.add_argument("--arm-delay-s", type=float, default=5.0)
    home.add_argument("--startup", action="store_true",
                      help="Use the tool startup profile: 10 mm clearance, no approach, reduced current.")

    move = sub.choices["move"]
    move.add_argument("--confirm", required=True)
    move.add_argument("--arm-delay-s", type=float, default=5.0)
    move.add_argument("--target-mm", type=float, required=True)
    move.add_argument("--rpm", type=int, help="Defaults to the configured Y rpm, which is also the ceiling.")
    return parser


async def async_main(args: argparse.Namespace) -> int:
    with base.acquire_ownership_lock(base.OWNERSHIP_LOCK_PATH):
        base.refuse_if_api_is_live(base.NORMAL_API_HOST, base.NORMAL_API_PORT)
        params = base.load_y_motor_params(args.config)
        controller = base.make_controller(args)
        await controller.connect()
        try:
            if args.command == "inspect":
                return await run_inspect(args, controller, params)
            if args.command == "move":
                return await run_move(args, controller, params)
            return await run_home(args, controller, params)
        finally:
            await controller.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        base.emit("interrupted")
        return 130
    except Exception as exc:
        base.emit("fatal", error=str(exc), error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
