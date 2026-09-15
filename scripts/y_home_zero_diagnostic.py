#!/usr/bin/env python3
"""Supervised diagnostic for the lb4a Y home-zero offset.

Reads commanded vs actual position, following error and alarm state at each
step of the production Motor.home() sequence, to show where a post-home
offset (one revolution, 40 mm) appears.

  inspect  read-only snapshots; run after the app has homed and been stopped
  home     runs Motor.configure() + Motor.home() with checkpoint reads

Checkpoint reads add ~100 ms of bus traffic between homing steps.
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

# (address, word count). Two-word entries are signed 32-bit, high word first.
REGISTERS = {
    "status": (0x1003, 1),
    "alarm": (0x2203, 1),
    "trigger": (0x6002, 1),
    "software_enable": (0x000F, 1),
    "motor_direction": (0x0007, 1),
    "following_error_limit": (0x000B, 1),
    "alarm_detection": (0x016D, 1),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default=base.DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
    parser.add_argument("--timeout", type=float, default=2.0)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("inspect", "home"):
        cmd = sub.add_parser(name)
        base.add_common_command_arguments(cmd)
        cmd.add_argument("--samples", type=int, default=10)
        cmd.add_argument("--interval-ms", type=float, default=100.0)

    home = sub.choices["home"]
    home.add_argument("--confirm", required=True)
    home.add_argument("--arm-delay-s", type=float, default=5.0)
    home.add_argument("--startup", action="store_true",
                      help="Use the tool startup profile: 10 mm clearance, no approach, reduced current.")
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
