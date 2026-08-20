#!/usr/bin/env python3
"""Exclusive, supervised diagnostics for lb4a-1 Y brake timing.

This tool deliberately has no homing, SO2, or EEPROM-persistence command. Its
only writes select one of the fixed 100/250 ms delay-matrix states below, and
its only motion commands use fixed ascending targets within 20 mm.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import json
import math
import os
import socket
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
for import_path in reversed(
    [
        REPO_ROOT / "openshelf_hardware/packages/openshelf_os/src",
        REPO_ROOT / "openshelf_hardware/packages/hardware/src",
    ]
):
    sys.path.insert(0, str(import_path))

from hardware.controller import SharedModbusController  # noqa: E402
from hardware.motor import Motor  # noqa: E402
from openshelf_os.common import load_and_validate_config  # noqa: E402


DEFAULT_PORT = os.getenv("OPENSHELF_MOTOR_PORT", "/dev/openshelf-rs485-ch1")
NORMAL_API_HOST = "127.0.0.1"
NORMAL_API_PORT = 8000
API_LIVE_CHECK_TIMEOUT_SECONDS = 0.2
OWNERSHIP_LOCK_PATH = REPO_ROOT / "openshelf_hardware/data/.y-brake-timing.lock"

SUPPORTED_REGISTER_MAP = "cl2b-rs507-pr"
Y_AXIS_UNIT = 2
MOTION_RUNNING_BIT = 2

# These are intentionally not CLI options.  The loaded vertical axis must stay
# close enough to the lower stop that a loss of holding torque has limited drop.
TEST_Y_MIN_MM = -1.0
TEST_Y_MAX_MM = 50.0
MAX_STATIONARY_DRIFT_MM = 0.02
MIN_SHORT_MOVE_MM = 0.5
SHORT_MOVE_RPM = 10
SHORT_MOVE_ACCEL = 100
SHORT_MOVE_TIMEOUT_SECONDS = 20.0

DEDICATED_BRAKE_NO = 0x24
BASELINE_BRAKE_DELAY_MS = 100
ALTERNATE_BRAKE_DELAY_MS = 250
BRAKE_SPEED_THRESHOLD = 10

DELAY_WRITE_CONFIRMATION = "SUPERVISED-RESTRAINED-Y-BRAKE-DELAY-CHANGE"
MOVE_CONFIRMATION = "SUPERVISED-RESTRAINED-Y-TIMING-MOVE"

REGISTERS = {
    "command_pulses_per_revolution": (0x0001, 1),
    "control_mode": (0x0003, 1),
    "status": (0x1003, 1),
    "alarm": (0x2203, 1),
    "position_block": (0x602C, 2),
    "so2_function": (0x0159, 1),
    "brake_release_delay_ms": (0x0167, 1),
    "brake_engagement_delay_ms": (0x0169, 1),
    "brake_engagement_speed_threshold": (0x016B, 1),
    "output_state": (0x017B, 1),
    "bus_voltage": (0x0177, 1),
    "peak_current_setting": (0x0191, 1),
    "encoder_resolution_cpr": (0x0233, 1),
}
DELAY_REGISTER_NAMES = {
    "release": "brake_release_delay_ms",
    "engagement": "brake_engagement_delay_ms",
}


@dataclass(frozen=True)
class DelayTransition:
    changed_delay: str
    before: tuple[int, int]
    after: tuple[int, int]


@dataclass(frozen=True)
class FixedMove:
    delays: tuple[int, int]
    target_mm: float


# Every write has one valid predecessor. This prevents skipping ahead or using
# the harness as a general register editor.
DELAY_TRANSITIONS = {
    "set-release-250": DelayTransition("release", (100, 100), (250, 100)),
    "restore-release-100": DelayTransition("release", (250, 100), (100, 100)),
    "set-engagement-250": DelayTransition("engagement", (100, 100), (100, 250)),
    "restore-engagement-100": DelayTransition("engagement", (100, 250), (100, 100)),
    "set-both-250": DelayTransition("release", (100, 250), (250, 250)),
    "restore-both-release-100": DelayTransition("release", (250, 250), (100, 250)),
}

FIXED_MOVES = {
    "move-release-250": FixedMove((250, 100), 10.0),
    "move-engagement-250": FixedMove((100, 250), 15.0),
    "move-both-250": FixedMove((250, 250), 20.0),
}

INSPECTION_PROFILES = {
    "baseline-100": (100, 100),
    "release-250": (250, 100),
    "engagement-250": (100, 250),
    "both-250": (250, 250),
}

# The diagnostic invokes Motor.move_config(), Motor.move_to_position(), and its
# wait loop directly.  Keep every parameter those paths use pinned to the
# stopped lb4a-1 Y-axis profile, so the logical 50 mm ceiling remains the
# physical direction and range we inspected.
CONFIRMED_Y_MOTOR_PARAMS = {
    "motor_name": "y_axis",
    "motor_id": Y_AXIS_UNIT,
    "dr": 0,
    "Ip": 60,
    "rpm": 150,
    "mpr": 40.0,
    "accel": 500,
    "ret_rpm": 64,
    "home_rpm": None,
    "lim": 1165,
    "lower_lim": 0,
    "phys_at_zero": None,
    "phys_at_max": None,
    "position_reg_high": 24620,
    "position_reg_low": 24621,
    "status_reg": 4099,
    "status_complete_bit": 5,
    "status_fault_bit": 0,
    "control_mode": 2,
    "ppr": 10000,
    "encoder_resolution_cpr": 4000,
    "max_following_error": 4000,
    "stall_timeout_s": 5.0,
    "brake_output_function": DEDICATED_BRAKE_NO,
    "brake_release_delay_ms": ALTERNATE_BRAKE_DELAY_MS,
    "brake_engagement_delay_ms": ALTERNATE_BRAKE_DELAY_MS,
    "brake_engagement_speed_threshold": BRAKE_SPEED_THRESHOLD,
}


@dataclass(frozen=True)
class DriveSnapshot:
    monotonic_s: float
    status: int
    alarm: int
    raw_position: int
    position_mm: float
    so2_function: int
    output_state: int
    brake_release_delay_ms: int
    brake_engagement_delay_ms: int
    brake_engagement_speed_threshold: int
    command_pulses_per_revolution: int
    control_mode: int
    peak_current_setting: int
    encoder_resolution_cpr: int
    bus_voltage: int


def raise_if_cancellation_requested() -> None:
    """Do not let PyModbus hide a Ctrl-C as a retryable transport failure."""
    current_task = asyncio.current_task()
    if current_task is not None and current_task.cancelling() > 0:
        raise asyncio.CancelledError()


class FailClosedSharedModbusController(SharedModbusController):
    """Diagnostic-only controller that never retries after a cancelled operation.

    PyModbus converts a cancelled transaction into ``ModbusIOException``.  The
    shared application controller treats that as transient and can reconnect
    and resend a request.  This harness uses one outer attempt instead, so a
    pending Ctrl-C always wins before another hardware operation can begin.
    """

    async def _run_with_reconnect_retry(
        self,
        operation: Any,
        *,
        attempts: int,
        failure_message: str,
        unexpected_message: str,
        retry_modbus_errors: bool = False,
        retry_log_context: dict[str, Any] | None = None,
    ) -> Any:
        del attempts, failure_message, unexpected_message, retry_modbus_errors, retry_log_context
        raise_if_cancellation_requested()
        try:
            result = await operation(self._require_connected())
        except asyncio.CancelledError:
            raise
        except Exception:
            raise_if_cancellation_requested()
            raise
        raise_if_cancellation_requested()
        return result


def emit(event: str, **fields: Any) -> None:
    print(
        json.dumps(
            {
                "timestamp": time.time(),
                "event": event,
                **fields,
            },
            sort_keys=True,
        ),
        flush=True,
    )


def add_common_command_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--register-map",
        choices=[SUPPORTED_REGISTER_MAP],
        required=True,
        help="Confirm the installed driver is the engineer-identified CL2B-RS507.",
    )
    parser.add_argument("--config", type=Path, required=True)


def add_sampling_arguments(
    parser: argparse.ArgumentParser,
    *,
    default_samples: int,
    default_interval_ms: float,
) -> None:
    parser.add_argument("--samples", type=int, default=default_samples)
    parser.add_argument("--sample-interval-ms", type=float, default=default_interval_ms)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Supervised CL2B-RS507 Y brake timing diagnostic. The normal API must "
            "be stopped; no command can home, change SO2, persist registers, or "
            "target Y above 20 mm."
        )
    )
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
    parser.add_argument("--timeout", type=float, default=2.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Read-only stationary sampling.")
    add_common_command_arguments(inspect)
    add_sampling_arguments(inspect, default_samples=20, default_interval_ms=50.0)
    inspect.add_argument("--expect", choices=sorted(INSPECTION_PROFILES), required=True)

    for command, transition in DELAY_TRANSITIONS.items():
        transition_parser = subparsers.add_parser(
            command,
            help=(
                f"One-shot {transition.changed_delay} delay transition "
                f"{transition.before[0]}/{transition.before[1]} -> "
                f"{transition.after[0]}/{transition.after[1]} ms."
            ),
        )
        add_common_command_arguments(transition_parser)
        add_sampling_arguments(
            transition_parser,
            default_samples=100,
            default_interval_ms=20.0,
        )
        transition_parser.add_argument("--arm-delay-s", type=float, default=5.0)
        transition_parser.add_argument("--confirm", required=True)

    for command, move in FIXED_MOVES.items():
        move_parser = subparsers.add_parser(
            command,
            help=(
                f"Move to the fixed {move.target_mm:g} mm target with delays "
                f"{move.delays[0]}/{move.delays[1]} ms."
            ),
        )
        add_common_command_arguments(move_parser)
        move_parser.add_argument("--arm-delay-s", type=float, default=5.0)
        move_parser.add_argument("--confirm", required=True)

    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.register_map != SUPPORTED_REGISTER_MAP:
        raise ValueError(
            f"pass --register-map {SUPPORTED_REGISTER_MAP} only after checking the driver label"
        )

    if args.command == "inspect" or args.command in DELAY_TRANSITIONS:
        if not 1 <= args.samples <= 500:
            raise ValueError("--samples must be between 1 and 500")
        if not 10.0 <= args.sample_interval_ms <= 1000.0:
            raise ValueError("--sample-interval-ms must be between 10 and 1000")

    if args.command in DELAY_TRANSITIONS:
        if args.confirm != DELAY_WRITE_CONFIRMATION:
            raise ValueError(
                "delay-write confirmation mismatch; this command is allowed only with "
                "independent mechanical restraint and onsite E-stop supervision"
            )
        validate_arm_delay(args.arm_delay_s)

    if args.command in FIXED_MOVES:
        if args.confirm != MOVE_CONFIRMATION:
            raise ValueError(
                "move confirmation mismatch; this command requires onsite E-stop supervision"
            )
        validate_arm_delay(args.arm_delay_s)


def validate_arm_delay(delay_s: float) -> None:
    if not math.isfinite(delay_s) or not 3.0 <= delay_s <= 30.0:
        raise ValueError("--arm-delay-s must be between 3 and 30 seconds")


def make_controller(args: argparse.Namespace) -> FailClosedSharedModbusController:
    return FailClosedSharedModbusController(
        port=args.port,
        baudrate=args.baudrate,
        parity=args.parity,
        timeout=args.timeout,
    )


def refuse_if_api_is_live(host: str, port: int) -> None:
    try:
        connection = socket.create_connection(
            (host, port),
            timeout=API_LIVE_CHECK_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return
        raise RuntimeError(
            f"cannot verify normal API is stopped on {host}:{port}: {exc}; "
            "refusing serial ownership"
        ) from exc
    else:
        connection.close()
        raise RuntimeError(
            f"normal API appears to be listening on {host}:{port}; stop it with the "
            "repo-managed scripts before running this harness"
        )


@contextmanager
def acquire_ownership_lock(lock_path: Path) -> Iterator[None]:
    if not lock_path.parent.is_dir():
        raise RuntimeError(f"ownership lock parent does not exist: {lock_path.parent}")

    try:
        lock_file = lock_path.open("a+")
    except OSError as exc:
        raise RuntimeError(f"cannot open ownership lock {lock_path}: {exc}") from exc

    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another Y brake harness holds {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def load_y_motor_params(config_path: Path) -> Any:
    try:
        raw_config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read configuration {config_path}: {exc}") from exc

    config = load_and_validate_config(raw_config, str(config_path))
    motor_params = next(
        (
            motor
            for motor in config.robot.tool.motors
            if motor.motor_name == "y_axis" and motor.enabled is True
        ),
        None,
    )
    if motor_params is None:
        raise RuntimeError(f"enabled y_axis not found in {config_path}")

    mismatches = {
        field: {
            "expected": value,
            "actual": getattr(motor_params, field, None),
        }
        for field, value in CONFIRMED_Y_MOTOR_PARAMS.items()
        if getattr(motor_params, field, None) != value
    }
    if mismatches:
        raise RuntimeError(
            "configured Y profile does not match the confirmed lb4a motor/driver: "
            + json.dumps(mismatches, sort_keys=True)
        )
    return motor_params


async def read_register(
    controller: SharedModbusController,
    name: str,
) -> tuple[int, ...]:
    address, count = REGISTERS[name]
    return await controller.read_holding_register_block(Y_AXIS_UNIT, address, count=count)


def signed_position(position: tuple[int, ...]) -> int:
    if len(position) != 2:
        raise RuntimeError(f"expected two position registers, received {len(position)}")
    raw = ((position[0] & 0xFFFF) << 16) | (position[1] & 0xFFFF)
    return raw - 0x1_0000_0000 if raw & 0x8000_0000 else raw


def raw_to_logical_mm(raw_position: int, command_ppr: int, motor_params: Any) -> float:
    if command_ppr != int(motor_params.ppr):
        raise RuntimeError(
            "live command pulses/revolution does not match config: "
            f"live={command_ppr}, config={motor_params.ppr}"
        )
    physical_mm = raw_position / command_ppr * float(motor_params.mpr)
    return float(motor_params.physical_to_logical(physical_mm))


async def capture_snapshot(
    controller: SharedModbusController,
    motor_params: Any,
) -> DriveSnapshot:
    position_block = await read_register(controller, "position_block")
    command_ppr = (await read_register(controller, "command_pulses_per_revolution"))[0]
    raw_position = signed_position(position_block)
    return DriveSnapshot(
        monotonic_s=time.monotonic(),
        status=(await read_register(controller, "status"))[0],
        alarm=(await read_register(controller, "alarm"))[0],
        raw_position=raw_position,
        position_mm=raw_to_logical_mm(raw_position, command_ppr, motor_params),
        so2_function=(await read_register(controller, "so2_function"))[0],
        output_state=(await read_register(controller, "output_state"))[0],
        brake_release_delay_ms=(
            await read_register(controller, "brake_release_delay_ms")
        )[0],
        brake_engagement_delay_ms=(
            await read_register(controller, "brake_engagement_delay_ms")
        )[0],
        brake_engagement_speed_threshold=(
            await read_register(controller, "brake_engagement_speed_threshold")
        )[0],
        command_pulses_per_revolution=command_ppr,
        control_mode=(await read_register(controller, "control_mode"))[0],
        peak_current_setting=(await read_register(controller, "peak_current_setting"))[0],
        encoder_resolution_cpr=(
            await read_register(controller, "encoder_resolution_cpr")
        )[0],
        bus_voltage=(await read_register(controller, "bus_voltage"))[0],
    )


def require_safe_snapshot(snapshot: DriveSnapshot) -> None:
    if snapshot.status & (1 << MOTION_RUNNING_BIT):
        raise RuntimeError(f"Y reports motion running: status=0x{snapshot.status:04X}")
    if snapshot.alarm != 0:
        raise RuntimeError(f"Y alarm is active: 0x{snapshot.alarm:04X}")
    if not TEST_Y_MIN_MM <= snapshot.position_mm <= TEST_Y_MAX_MM:
        raise RuntimeError(
            "Y is outside the hardcoded diagnostic window: "
            f"position={snapshot.position_mm:.6f} mm, "
            f"allowed={TEST_Y_MIN_MM:.1f}..{TEST_Y_MAX_MM:.1f} mm"
        )


def require_live_profile(
    snapshot: DriveSnapshot,
    *,
    expected_delays: tuple[int, int],
) -> None:
    expected = {
        "so2_function": DEDICATED_BRAKE_NO,
        "brake_release_delay_ms": expected_delays[0],
        "brake_engagement_delay_ms": expected_delays[1],
        "brake_engagement_speed_threshold": BRAKE_SPEED_THRESHOLD,
        "command_pulses_per_revolution": 10000,
        "control_mode": 2,
        "peak_current_setting": 60,
        "encoder_resolution_cpr": 4000,
    }
    mismatches = {
        field: {"expected": value, "actual": getattr(snapshot, field)}
        for field, value in expected.items()
        if getattr(snapshot, field) != value
    }
    if mismatches:
        raise RuntimeError(
            "live Y profile does not match the required timing-test state: "
            + json.dumps(mismatches, sort_keys=True)
        )


def require_no_position_change(
    baseline: DriveSnapshot,
    current: DriveSnapshot,
    *,
    context: str,
) -> None:
    drift = abs(current.position_mm - baseline.position_mm)
    if drift > MAX_STATIONARY_DRIFT_MM:
        raise RuntimeError(
            f"Y position changed during {context}: baseline={baseline.position_mm:.6f} mm, "
            f"current={current.position_mm:.6f} mm, drift={drift:.6f} mm"
        )


async def require_stationary_healthy_y(
    controller: SharedModbusController,
    motor_params: Any,
) -> DriveSnapshot:
    first = await capture_snapshot(controller, motor_params)
    require_safe_snapshot(first)
    await asyncio.sleep(0.25)
    second = await capture_snapshot(controller, motor_params)
    require_safe_snapshot(second)
    require_no_position_change(first, second, context="stationary preflight")
    return second


async def arm_countdown(command: str, delay_s: float) -> None:
    emit(
        "armed",
        command=command,
        delay_s=delay_s,
        requirement=(
            "vertical load independently restrained; onsite engineer at E-stop; "
            "abort on brake chatter, vibration, drop, or unexpected motion"
        ),
    )
    deadline = time.monotonic() + delay_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        emit("countdown", command=command, remaining_s=round(remaining, 3))
        await asyncio.sleep(min(1.0, remaining))


async def write_delay_once(
    controller: SharedModbusController,
    *,
    delay_name: str,
    value: int,
) -> str | None:
    """Send one fixed volatile delay write without retrying it.

    A missing response is ambiguous: the drive may have applied the request, so
    the caller must read the delay back instead of sending it again.
    """
    register_name = DELAY_REGISTER_NAMES[delay_name]
    register_address = REGISTERS[register_name][0]
    raise_if_cancellation_requested()
    async with controller.lock:
        client = controller.client
        if client is None or client.connected is not True:
            raise RuntimeError("Modbus client is not connected for the one-shot delay write")

        transaction_manager = client.ctx
        original_retries = transaction_manager.retries
        transaction_manager.retries = 0
        try:
            result = await client.write_register(
                address=register_address,
                value=value,
                device_id=Y_AXIS_UNIT,
            )
        except Exception as exc:
            raise_if_cancellation_requested()
            return f"{type(exc).__name__}: {exc}"
        finally:
            transaction_manager.retries = original_retries

    raise_if_cancellation_requested()
    if result is None:
        return "no response"
    if result.isError():
        raise RuntimeError(f"delay write rejected by unit {Y_AXIS_UNIT}: {result}")
    return None


async def run_inspect(
    args: argparse.Namespace,
    controller: SharedModbusController,
    motor_params: Any,
) -> int:
    expected_delays = INSPECTION_PROFILES[args.expect]
    baseline = await require_stationary_healthy_y(controller, motor_params)
    require_live_profile(baseline, expected_delays=expected_delays)
    emit("inspection_preflight", expected_profile=args.expect, snapshot=asdict(baseline))
    for sample in range(args.samples):
        snapshot = await capture_snapshot(controller, motor_params)
        require_safe_snapshot(snapshot)
        require_no_position_change(baseline, snapshot, context="inspection sampling")
        require_live_profile(snapshot, expected_delays=expected_delays)
        emit("inspection_sample", sample=sample, snapshot=asdict(snapshot))
        if sample + 1 < args.samples:
            await asyncio.sleep(args.sample_interval_ms / 1000.0)
    emit("inspection_complete", samples=args.samples, writes=0)
    return 0


async def run_delay_transition(
    args: argparse.Namespace,
    controller: SharedModbusController,
    motor_params: Any,
) -> int:
    transition = DELAY_TRANSITIONS[args.command]
    baseline = await require_stationary_healthy_y(controller, motor_params)
    require_live_profile(baseline, expected_delays=transition.before)
    emit(
        "delay_transition_preflight",
        changed_delay=transition.changed_delay,
        before=transition.before,
        after=transition.after,
        snapshot=asdict(baseline),
    )
    await arm_countdown(args.command, args.arm_delay_s)

    immediate = await require_stationary_healthy_y(controller, motor_params)
    require_live_profile(immediate, expected_delays=transition.before)
    require_no_position_change(baseline, immediate, context="transition arm delay")

    register_name = DELAY_REGISTER_NAMES[transition.changed_delay]
    new_value = (
        transition.after[0]
        if transition.changed_delay == "release"
        else transition.after[1]
    )
    emit(
        "delay_write_imminent",
        unit=Y_AXIS_UNIT,
        address=REGISTERS[register_name][0],
        delay_name=transition.changed_delay,
        from_value=(
            transition.before[0]
            if transition.changed_delay == "release"
            else transition.before[1]
        ),
        to_value=new_value,
    )
    write_started = time.monotonic()
    ambiguous_response = await write_delay_once(
        controller,
        delay_name=transition.changed_delay,
        value=new_value,
    )
    emit(
        "delay_write_returned",
        elapsed_s=time.monotonic() - write_started,
        response_confirmed=ambiguous_response is None,
    )
    if ambiguous_response is not None:
        emit("delay_write_ambiguous", detail=ambiguous_response)
        try:
            delay_readback = (await read_register(controller, register_name))[0]
        except Exception as exc:
            emit("delay_write_readback", took_effect=None, error=f"{type(exc).__name__}: {exc}")
            raise RuntimeError(
                "delay write response was ambiguous and readback failed; the new value "
                "may or may not have taken effect"
            ) from exc
        took_effect = delay_readback == new_value
        emit(
            "delay_write_readback",
            delay_name=transition.changed_delay,
            delay_ms=delay_readback,
            took_effect=took_effect,
        )
        if not took_effect:
            raise RuntimeError(
                "delay write response was ambiguous and readback did not confirm the "
                f"new value: expected={new_value}, actual={delay_readback}"
            )

    for sample in range(args.samples):
        snapshot = await capture_snapshot(controller, motor_params)
        require_safe_snapshot(snapshot)
        require_no_position_change(immediate, snapshot, context="delay transition sampling")
        require_live_profile(snapshot, expected_delays=transition.after)
        emit("delay_transition_sample", sample=sample, snapshot=asdict(snapshot))
        if sample + 1 < args.samples:
            await asyncio.sleep(args.sample_interval_ms / 1000.0)

    emit(
        "delay_transition_complete",
        writes=1,
        changed_delay=transition.changed_delay,
        release_delay_ms=transition.after[0],
        engagement_delay_ms=transition.after[1],
        persisted=False,
    )
    return 0


async def stop_after_motion_error(motor: Motor, original_error: BaseException) -> None:
    stop_task = asyncio.create_task(motor.stop())
    while not stop_task.done():
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break

    try:
        stop_task.result()
    except BaseException as stop_error:
        original_error.add_note(f"emergency Y stop also failed: {stop_error!r}")
        setattr(original_error, "stop_error", stop_error)


async def run_fixed_move(
    args: argparse.Namespace,
    controller: SharedModbusController,
    motor_params: Any,
) -> int:
    move = FIXED_MOVES[args.command]
    target_mm = move.target_mm
    baseline = await require_stationary_healthy_y(controller, motor_params)
    require_live_profile(baseline, expected_delays=move.delays)
    if target_mm < baseline.position_mm + MIN_SHORT_MOVE_MM:
        raise RuntimeError(
            "fixed timing move must move away from the lower hard stop by at least "
            f"{MIN_SHORT_MOVE_MM:g} mm: current={baseline.position_mm:.6f}, "
            f"target={target_mm:.6f}"
        )
    if target_mm > 20.0 or target_mm > TEST_Y_MAX_MM:
        raise RuntimeError("fixed timing move target exceeds its hardcoded 20 mm ceiling")

    emit(
        "fixed_move_preflight",
        snapshot=asdict(baseline),
        target_mm=target_mm,
        release_delay_ms=move.delays[0],
        engagement_delay_ms=move.delays[1],
        rpm=SHORT_MOVE_RPM,
        accel=SHORT_MOVE_ACCEL,
    )
    await arm_countdown(args.command, args.arm_delay_s)

    immediate = await require_stationary_healthy_y(controller, motor_params)
    require_live_profile(immediate, expected_delays=move.delays)
    require_no_position_change(baseline, immediate, context="move arm delay")

    motor = Motor(controller, motor_params)
    motor._cur_pos = immediate.position_mm
    motor._last_raw_position = immediate.raw_position

    try:
        await motor.move_config()

        final_preflight = await require_stationary_healthy_y(controller, motor_params)
        require_live_profile(final_preflight, expected_delays=move.delays)
        require_no_position_change(immediate, final_preflight, context="motion configuration")
        if target_mm < final_preflight.position_mm + MIN_SHORT_MOVE_MM:
            raise RuntimeError(
                "fixed timing move must move away from the lower hard stop by at least "
                f"{MIN_SHORT_MOVE_MM:g} mm: current={final_preflight.position_mm:.6f}, "
                f"target={target_mm:.6f}"
            )
        emit("fixed_move_trigger_imminent", target_mm=target_mm)
        async with asyncio.timeout(SHORT_MOVE_TIMEOUT_SECONDS):
            await motor.move_to_position(
                target_mm,
                rpm=SHORT_MOVE_RPM,
                accel=SHORT_MOVE_ACCEL,
            )

        final = await require_stationary_healthy_y(controller, motor_params)
        require_live_profile(final, expected_delays=move.delays)
        if abs(final.position_mm - target_mm) > 1.0:
            raise RuntimeError(
                "fixed timing move ended outside target tolerance: "
                f"target={target_mm:.6f}, actual={final.position_mm:.6f}"
            )
    except BaseException as exc:
        await stop_after_motion_error(motor, exc)
        raise

    emit(
        "fixed_move_complete",
        initial_position_mm=baseline.position_mm,
        final_position_mm=final.position_mm,
        target_mm=target_mm,
        ceiling_mm=TEST_Y_MAX_MM,
        persisted=False,
    )
    return 0


async def async_main(args: argparse.Namespace) -> int:
    validate_args(args)
    with acquire_ownership_lock(OWNERSHIP_LOCK_PATH):
        refuse_if_api_is_live(NORMAL_API_HOST, NORMAL_API_PORT)
        motor_params = load_y_motor_params(args.config)
        controller = make_controller(args)
        await controller.connect()
        try:
            if args.command == "inspect":
                return await run_inspect(args, controller, motor_params)
            if args.command in DELAY_TRANSITIONS:
                return await run_delay_transition(args, controller, motor_params)
            return await run_fixed_move(args, controller, motor_params)
        finally:
            await controller.close()


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        emit("interrupted")
        return 130
    except Exception as exc:
        emit("fatal", error=str(exc), error_type=type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
