#!/usr/bin/env python3
"""Supervised, bounded mapping of the lb4a Y limit GPIO inputs.

The confirmed scan starts near the lower stop, moves to 100 mm, then approaches
zero through fixed targets and stops when PCA9535_0 P04 returns to its starting
state. P05 is sampled alongside it. The tool never homes, changes SO2, or saves
driver EEPROM.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sys
import time
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import y_brake_timing_diagnostic as timing  # noqa: E402
from hardware.gpio import GpioManager  # noqa: E402
from hardware.gpio.pca9535_mod import PCA9535  # noqa: E402
from hardware.motor import Motor  # noqa: E402
from openshelf_os.common import load_and_validate_config  # noqa: E402


OWNERSHIP_LOCK_PATH = (
    timing.REPO_ROOT / "openshelf_hardware/data/.y-home-zone-diagnostic.lock"
)
EXPECTED_EXPANDER_NAME = "pca9535_0"
EXPECTED_EXPANDER_BUS = 1
EXPECTED_EXPANDER_ADDRESS = 0x20
EXPECTED_RAW_YMIN_PIN = 4
EXPECTED_RAW_YMAX_PIN = 5
EXPECTED_DELAYS = (250, 250)

START_Y_MIN_MM = -1.0
START_Y_MAX_MM = 1.0
MAX_Y_MM = 10.0
EXTENDED_AWAY_TARGET_MM = 100.0
EXTENDED_AWAY_START_MIN_MM = 9.0
EXTENDED_AWAY_START_MAX_MM = 11.0
HOMEWARD_START_MIN_MM = 99.0
HOMEWARD_START_MAX_MM = 101.0
HOMEWARD_TARGETS_MM = (50.0, 20.0, 10.0, 5.0, 2.0, 1.0, 0.5, 0.25, 0.0)
EXTENDED_Y_MIN_MM = -1.0
EXTENDED_Y_MAX_MM = 101.0
SECONDARY_EXPANDER_ADDRESS = 0x21
AWAY_STEP_MM = 1.0
RETURN_STEP_MM = 0.5
MIN_MOVE_MM = 0.2
MOVE_RPM = 10
MOVE_ACCEL = 100
MOVE_TIMEOUT_SECONDS = 20.0
GPIO_STABLE_SAMPLES = 5
GPIO_SAMPLE_INTERVAL_SECONDS = 0.02

SCAN_CONFIRMATION = "SUPERVISED-RESTRAINED-Y-HOME-ZONE-SCAN"
EXTENDED_MOVE_CONFIRMATION = "SUPERVISED-RESTRAINED-Y-HOME-ZONE-100MM-MOVE"
HOMEWARD_SCAN_CONFIRMATION = "SUPERVISED-RESTRAINED-Y-HOMEWARD-LIMIT-SCAN"
CONFIRMED_LIMIT_SCAN_CONFIRMATION = (
    "SUPERVISED-RESTRAINED-Y-CONFIRMED-LIMIT-SCAN"
)


@dataclass(frozen=True)
class ExpanderTarget:
    bus: int
    address: int
    configured_y_min_name: str
    configured_y_min_pin: int
    configured_y_max_name: str
    configured_y_max_pin: int


@dataclass(frozen=True)
class GpioSnapshot:
    monotonic_s: float
    port0: int
    port1: int
    y_min_high: bool
    y_max_high: bool


@dataclass(frozen=True)
class LogicalLimitSnapshot:
    monotonic_s: float
    y_min: bool
    y_max: bool


def emit(event: str, **fields: Any) -> None:
    timing.emit(event, **fields)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read or safely map PCA9535_0 P04/P05 as the lb4a Y limit inputs. "
            "The normal API must be stopped."
        )
    )
    parser.add_argument("--port", default=timing.DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
    parser.add_argument("--timeout", type=float, default=2.0)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Read-only Y and raw GPIO snapshot.")
    timing.add_common_command_arguments(inspect)

    scan = subparsers.add_parser(
        "scan-zone",
        help="Bounded 0..10 mm away/return scan of raw P04.",
    )
    timing.add_common_command_arguments(scan)
    scan.add_argument("--arm-delay-s", type=float, default=5.0)
    scan.add_argument("--confirm", required=True)

    extended = subparsers.add_parser(
        "move-away-100",
        help="Fixed low-speed move from approximately 10 mm to exactly 100 mm.",
    )
    timing.add_common_command_arguments(extended)
    extended.add_argument("--arm-delay-s", type=float, default=5.0)
    extended.add_argument("--confirm", required=True)

    homeward = subparsers.add_parser(
        "scan-toward-home",
        help="Fixed low-speed scan from approximately 100 mm toward 0 mm.",
    )
    timing.add_common_command_arguments(homeward)
    homeward.add_argument("--arm-delay-s", type=float, default=5.0)
    homeward.add_argument("--confirm", required=True)

    confirmed_limits = subparsers.add_parser(
        "scan-confirmed-limits",
        help="Fixed low-speed P04/P05 scan from 0 to 100 mm and back toward 0.",
    )
    timing.add_common_command_arguments(confirmed_limits)
    confirmed_limits.add_argument("--arm-delay-s", type=float, default=5.0)
    confirmed_limits.add_argument("--confirm", required=True)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.register_map != timing.SUPPORTED_REGISTER_MAP:
        raise ValueError(
            f"pass --register-map {timing.SUPPORTED_REGISTER_MAP} only after checking "
            "the driver label"
        )
    if args.command == "scan-zone":
        if args.confirm != SCAN_CONFIRMATION:
            raise ValueError(
                "scan confirmation mismatch; this command requires independent load "
                "restraint and onsite E-stop supervision"
            )
        timing.validate_arm_delay(args.arm_delay_s)
    if args.command == "move-away-100":
        if args.confirm != EXTENDED_MOVE_CONFIRMATION:
            raise ValueError(
                "100 mm move confirmation mismatch; this command requires independent "
                "load restraint and onsite E-stop supervision"
            )
        timing.validate_arm_delay(args.arm_delay_s)
    if args.command == "scan-toward-home":
        if args.confirm != HOMEWARD_SCAN_CONFIRMATION:
            raise ValueError(
                "homeward scan confirmation mismatch; this command requires independent "
                "load restraint and onsite E-stop supervision"
            )
        timing.validate_arm_delay(args.arm_delay_s)
    if args.command == "scan-confirmed-limits":
        if args.confirm != CONFIRMED_LIMIT_SCAN_CONFIRMATION:
            raise ValueError(
                "confirmed limit scan confirmation mismatch; this command requires "
                "independent load restraint and onsite E-stop supervision"
            )
        timing.validate_arm_delay(args.arm_delay_s)


def load_expander_target(config_path: Path) -> ExpanderTarget:
    try:
        raw_config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read configuration {config_path}: {exc}") from exc

    config = load_and_validate_config(raw_config, str(config_path))
    expanders = {expander.name: expander for expander in config.robot.gpio.expanders}
    try:
        expander = expanders[EXPECTED_EXPANDER_NAME]
    except KeyError as exc:
        raise RuntimeError(f"missing GPIO expander {EXPECTED_EXPANDER_NAME}") from exc

    if (
        expander.type != "pca9535"
        or expander.bus != EXPECTED_EXPANDER_BUS
        or expander.address != EXPECTED_EXPANDER_ADDRESS
    ):
        raise RuntimeError(
            "unexpected home-zone expander configuration: "
            + json.dumps(expander.model_dump(), sort_keys=True)
        )

    pins = config.robot.gpio.pins
    y_min_names = sorted(
        name
        for name, spec in pins.items()
        if spec.board == "pca9535"
        and spec.expander == EXPECTED_EXPANDER_NAME
        and spec.pin == EXPECTED_RAW_YMIN_PIN
    )
    y_max_names = sorted(
        name
        for name, spec in pins.items()
        if spec.board == "pca9535"
        and spec.expander == EXPECTED_EXPANDER_NAME
        and spec.pin == EXPECTED_RAW_YMAX_PIN
    )
    if y_min_names != ["y_lim_min"]:
        raise RuntimeError(
            f"expected y_lim_min on raw P04, found configured names: {y_min_names}"
        )
    if y_max_names != ["y_lim_max"]:
        raise RuntimeError(
            f"expected y_lim_max on raw P05, found configured names: {y_max_names}"
        )
    try:
        y_min = pins["y_lim_min"]
        y_max = pins["y_lim_max"]
    except KeyError as exc:
        raise RuntimeError("software configuration is missing a Y limit input") from exc

    return ExpanderTarget(
        bus=expander.bus,
        address=expander.address,
        configured_y_min_name="y_lim_min",
        configured_y_min_pin=y_min.pin,
        configured_y_max_name="y_lim_max",
        configured_y_max_pin=y_max.pin,
    )


def capture_gpio(device: PCA9535) -> GpioSnapshot:
    port0, port1 = device.port_input_read()
    return GpioSnapshot(
        monotonic_s=time.monotonic(),
        port0=port0,
        port1=port1,
        y_min_high=bool(port0 & (1 << EXPECTED_RAW_YMIN_PIN)),
        y_max_high=bool(port0 & (1 << EXPECTED_RAW_YMAX_PIN)),
    )


async def capture_stable_gpio(device: PCA9535) -> GpioSnapshot:
    samples: list[GpioSnapshot] = []
    for sample_index in range(GPIO_STABLE_SAMPLES):
        samples.append(capture_gpio(device))
        if sample_index + 1 < GPIO_STABLE_SAMPLES:
            await asyncio.sleep(GPIO_SAMPLE_INTERVAL_SECONDS)

    states = {(sample.port0, sample.port1) for sample in samples}
    if len(states) != 1:
        raise RuntimeError(
            "GPIO inputs were not stable while Y was stopped: "
            + json.dumps([asdict(sample) for sample in samples], sort_keys=True)
        )
    return samples[-1]


async def capture_stable_logical_limits(
    y_min_input: Any,
    y_max_input: Any,
) -> LogicalLimitSnapshot:
    samples: list[LogicalLimitSnapshot] = []
    for sample_index in range(GPIO_STABLE_SAMPLES):
        samples.append(
            LogicalLimitSnapshot(
                monotonic_s=time.monotonic(),
                y_min=bool(y_min_input.read()),
                y_max=bool(y_max_input.read()),
            )
        )
        if sample_index + 1 < GPIO_STABLE_SAMPLES:
            await asyncio.sleep(GPIO_SAMPLE_INTERVAL_SECONDS)

    states = {(sample.y_min, sample.y_max) for sample in samples}
    if len(states) != 1:
        raise RuntimeError(
            "application GPIO limit inputs were not stable while Y was stopped: "
            + json.dumps([asdict(sample) for sample in samples], sort_keys=True)
        )
    return samples[-1]


def load_application_gpio_manager(config_path: Path) -> GpioManager:
    try:
        raw_config = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read configuration {config_path}: {exc}") from exc
    config = load_and_validate_config(raw_config, str(config_path))
    return GpioManager.from_config(config.robot.gpio)


def changed_pins(before: GpioSnapshot, after: GpioSnapshot) -> list[int]:
    before_word = before.port0 | (before.port1 << 8)
    after_word = after.port0 | (after.port1 << 8)
    changed = before_word ^ after_word
    return [pin for pin in range(16) if changed & (1 << pin)]


def require_scan_start(position_mm: float) -> None:
    if not math.isfinite(position_mm) or not START_Y_MIN_MM <= position_mm <= START_Y_MAX_MM:
        raise RuntimeError(
            "zone scan must start close to the lower stop: "
            f"position={position_mm:.6f} mm, allowed={START_Y_MIN_MM:g}..{START_Y_MAX_MM:g} mm"
        )


def require_extended_move_start(position_mm: float) -> None:
    if (
        not math.isfinite(position_mm)
        or not EXTENDED_AWAY_START_MIN_MM
        <= position_mm
        <= EXTENDED_AWAY_START_MAX_MM
    ):
        raise RuntimeError(
            "100 mm move must start at the completed 10 mm scan bound: "
            f"position={position_mm:.6f} mm, "
            f"allowed={EXTENDED_AWAY_START_MIN_MM:g}..{EXTENDED_AWAY_START_MAX_MM:g} mm"
        )


def require_homeward_scan_start(position_mm: float) -> None:
    if (
        not math.isfinite(position_mm)
        or not HOMEWARD_START_MIN_MM <= position_mm <= HOMEWARD_START_MAX_MM
    ):
        raise RuntimeError(
            "homeward scan must start at the completed 100 mm move: "
            f"position={position_mm:.6f} mm, "
            f"allowed={HOMEWARD_START_MIN_MM:g}..{HOMEWARD_START_MAX_MM:g} mm"
        )


async def capture_other_expander(bus: int, address: int) -> GpioSnapshot:
    device = PCA9535(bus_num=bus, address=address)
    try:
        return await capture_stable_gpio(device)
    finally:
        device.close()


def require_extended_safe_snapshot(snapshot: timing.DriveSnapshot) -> None:
    if snapshot.status & (1 << timing.MOTION_RUNNING_BIT):
        raise RuntimeError(f"Y reports motion running: status=0x{snapshot.status:04X}")
    if snapshot.alarm != 0:
        raise RuntimeError(f"Y alarm is active: 0x{snapshot.alarm:04X}")
    if not EXTENDED_Y_MIN_MM <= snapshot.position_mm <= EXTENDED_Y_MAX_MM:
        raise RuntimeError(
            "Y is outside the fixed 100 mm diagnostic window: "
            f"position={snapshot.position_mm:.6f} mm, "
            f"allowed={EXTENDED_Y_MIN_MM:.1f}..{EXTENDED_Y_MAX_MM:.1f} mm"
        )


async def require_extended_stationary_healthy_y(
    controller: timing.SharedModbusController,
    motor_params: Any,
) -> timing.DriveSnapshot:
    first = await timing.capture_snapshot(controller, motor_params)
    require_extended_safe_snapshot(first)
    await asyncio.sleep(0.25)
    second = await timing.capture_snapshot(controller, motor_params)
    require_extended_safe_snapshot(second)
    timing.require_no_position_change(first, second, context="stationary preflight")
    return second


def away_targets(start_mm: float) -> list[float]:
    targets: list[float] = []
    target = AWAY_STEP_MM
    while target <= MAX_Y_MM + 1e-9:
        if target >= start_mm + MIN_MOVE_MM:
            targets.append(target)
        target += AWAY_STEP_MM
    return targets


def return_targets(away_mm: float, start_mm: float) -> list[float]:
    targets: list[float] = []
    target = away_mm - RETURN_STEP_MM
    while target > start_mm + MIN_MOVE_MM:
        targets.append(round(target, 6))
        target -= RETURN_STEP_MM
    if away_mm - start_mm >= MIN_MOVE_MM:
        targets.append(start_mm)
    return targets


async def move_and_capture(
    motor: Motor,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    *,
    target_mm: float,
    phase: str,
) -> tuple[timing.DriveSnapshot, GpioSnapshot]:
    emit("zone_move_trigger_imminent", phase=phase, target_mm=target_mm)
    async with asyncio.timeout(MOVE_TIMEOUT_SECONDS):
        await motor.move_to_position(target_mm, rpm=MOVE_RPM, accel=MOVE_ACCEL)
    drive = await timing.require_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(drive, expected_delays=EXPECTED_DELAYS)
    gpio = await capture_stable_gpio(gpio_device)
    emit(
        "zone_move_sample",
        phase=phase,
        target_mm=target_mm,
        drive=asdict(drive),
        gpio=asdict(gpio),
    )
    return drive, gpio


async def move_extended_and_capture(
    motor: Motor,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
) -> tuple[timing.DriveSnapshot, GpioSnapshot]:
    emit(
        "zone_move_trigger_imminent",
        phase="extended-away",
        target_mm=EXTENDED_AWAY_TARGET_MM,
    )
    async with asyncio.timeout(MOVE_TIMEOUT_SECONDS):
        await motor.move_to_position(
            EXTENDED_AWAY_TARGET_MM,
            rpm=MOVE_RPM,
            accel=MOVE_ACCEL,
        )
    drive = await require_extended_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(drive, expected_delays=EXPECTED_DELAYS)
    gpio = await capture_stable_gpio(gpio_device)
    emit(
        "zone_move_sample",
        phase="extended-away",
        target_mm=EXTENDED_AWAY_TARGET_MM,
        drive=asdict(drive),
        gpio=asdict(gpio),
    )
    return drive, gpio


async def move_homeward_and_capture(
    motor: Motor,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    *,
    target_mm: float,
) -> tuple[timing.DriveSnapshot, GpioSnapshot]:
    if target_mm not in HOMEWARD_TARGETS_MM:
        raise RuntimeError(f"target is not in the fixed homeward scan: {target_mm}")
    emit("zone_move_trigger_imminent", phase="toward-home", target_mm=target_mm)
    async with asyncio.timeout(MOVE_TIMEOUT_SECONDS):
        await motor.move_to_position(target_mm, rpm=MOVE_RPM, accel=MOVE_ACCEL)
    drive = await require_extended_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(drive, expected_delays=EXPECTED_DELAYS)
    if abs(drive.position_mm - target_mm) > 0.2:
        raise RuntimeError(
            "homeward scan move ended outside target tolerance: "
            f"target={target_mm:.6f}, actual={drive.position_mm:.6f}"
        )
    gpio = await capture_stable_gpio(gpio_device)
    emit(
        "zone_move_sample",
        phase="toward-home",
        target_mm=target_mm,
        drive=asdict(drive),
        gpio=asdict(gpio),
    )
    return drive, gpio


async def run_inspect(
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    target: ExpanderTarget,
) -> int:
    drive = await timing.require_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(drive, expected_delays=EXPECTED_DELAYS)
    gpio = await capture_stable_gpio(gpio_device)
    emit(
        "home_zone_inspection",
        drive=asdict(drive),
        gpio=asdict(gpio),
        expander=asdict(target),
        writes=0,
        motion_requests=0,
    )
    return 0


async def run_scan(
    args: argparse.Namespace,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    target: ExpanderTarget,
) -> int:
    baseline_drive = await timing.require_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(baseline_drive, expected_delays=EXPECTED_DELAYS)
    require_scan_start(baseline_drive.position_mm)
    baseline_gpio = await capture_stable_gpio(gpio_device)
    emit(
        "home_zone_scan_preflight",
        drive=asdict(baseline_drive),
        gpio=asdict(baseline_gpio),
        expander=asdict(target),
        max_y_mm=MAX_Y_MM,
        rpm=MOVE_RPM,
        accel=MOVE_ACCEL,
    )
    await timing.arm_countdown(args.command, args.arm_delay_s)

    final_preflight = await timing.require_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(final_preflight, expected_delays=EXPECTED_DELAYS)
    require_scan_start(final_preflight.position_mm)
    timing.require_no_position_change(
        baseline_drive,
        final_preflight,
        context="home-zone scan arm delay",
    )
    final_preflight_gpio = await capture_stable_gpio(gpio_device)
    if final_preflight_gpio.y_min_high != baseline_gpio.y_min_high:
        raise RuntimeError("raw P04 changed during the arm delay; refusing motion")

    motor = Motor(controller, motor_params)
    motor._cur_pos = final_preflight.position_mm
    motor._last_raw_position = final_preflight.raw_position
    try:
        await motor.move_config()
        configured_drive = await timing.require_stationary_healthy_y(controller, motor_params)
        timing.require_live_profile(configured_drive, expected_delays=EXPECTED_DELAYS)
        timing.require_no_position_change(
            final_preflight,
            configured_drive,
            context="home-zone motion configuration",
        )

        clear_drive: timing.DriveSnapshot | None = None
        clear_gpio: GpioSnapshot | None = None
        for target_mm in away_targets(configured_drive.position_mm):
            drive, gpio = await move_and_capture(
                motor,
                controller,
                motor_params,
                gpio_device,
                target_mm=target_mm,
                phase="away",
            )
            if gpio.y_min_high != baseline_gpio.y_min_high:
                clear_drive = drive
                clear_gpio = gpio
                break

        if clear_drive is None or clear_gpio is None:
            emit(
                "home_zone_not_cleared",
                initial_gpio=asdict(baseline_gpio),
                ceiling_mm=MAX_Y_MM,
            )
            raise RuntimeError(
                f"raw P04 did not change by {MAX_Y_MM:g} mm; Y was left at the safe upper scan bound"
            )

        emit(
            "home_zone_cleared",
            position_mm=clear_drive.position_mm,
            gpio=asdict(clear_gpio),
            changed_pins=changed_pins(baseline_gpio, clear_gpio),
        )

        asserted_drive: timing.DriveSnapshot | None = None
        asserted_gpio: GpioSnapshot | None = None
        for target_mm in return_targets(clear_drive.position_mm, configured_drive.position_mm):
            drive, gpio = await move_and_capture(
                motor,
                controller,
                motor_params,
                gpio_device,
                target_mm=target_mm,
                phase="return",
            )
            if gpio.y_min_high == baseline_gpio.y_min_high:
                asserted_drive = drive
                asserted_gpio = gpio
                break

        if asserted_drive is None or asserted_gpio is None:
            raise RuntimeError(
                "raw P04 did not return to its initial state before the initial Y position; "
                "no motion below the starting position was attempted"
            )
    except BaseException as exc:
        await timing.stop_after_motion_error(motor, exc)
        raise

    emit(
        "home_zone_scan_complete",
        initial_position_mm=baseline_drive.position_mm,
        clear_position_mm=clear_drive.position_mm,
        reassert_position_mm=asserted_drive.position_mm,
        initial_y_min_high=baseline_gpio.y_min_high,
        clear_y_min_high=clear_gpio.y_min_high,
        reassert_y_min_high=asserted_gpio.y_min_high,
        changed_pins=changed_pins(baseline_gpio, clear_gpio),
        ceiling_mm=MAX_Y_MM,
        persisted=False,
        homes=0,
    )
    return 0


async def run_extended_move(
    args: argparse.Namespace,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    target: ExpanderTarget,
) -> int:
    baseline_drive = await require_extended_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(baseline_drive, expected_delays=EXPECTED_DELAYS)
    require_extended_move_start(baseline_drive.position_mm)
    baseline_primary = await capture_stable_gpio(gpio_device)
    baseline_secondary = await capture_other_expander(
        target.bus, SECONDARY_EXPANDER_ADDRESS
    )
    emit(
        "extended_move_preflight",
        drive=asdict(baseline_drive),
        primary_gpio=asdict(baseline_primary),
        secondary_gpio=asdict(baseline_secondary),
        target_mm=EXTENDED_AWAY_TARGET_MM,
        rpm=MOVE_RPM,
        accel=MOVE_ACCEL,
    )
    await timing.arm_countdown(args.command, args.arm_delay_s)

    final_preflight = await require_extended_stationary_healthy_y(
        controller, motor_params
    )
    timing.require_live_profile(final_preflight, expected_delays=EXPECTED_DELAYS)
    require_extended_move_start(final_preflight.position_mm)
    timing.require_no_position_change(
        baseline_drive,
        final_preflight,
        context="100 mm move arm delay",
    )

    motor = Motor(controller, motor_params)
    motor._cur_pos = final_preflight.position_mm
    motor._last_raw_position = final_preflight.raw_position
    try:
        await motor.move_config()
        configured_drive = await require_extended_stationary_healthy_y(
            controller, motor_params
        )
        timing.require_live_profile(configured_drive, expected_delays=EXPECTED_DELAYS)
        timing.require_no_position_change(
            final_preflight,
            configured_drive,
            context="100 mm motion configuration",
        )
        final_drive, final_primary = await move_extended_and_capture(
            motor,
            controller,
            motor_params,
            gpio_device,
        )
        final_secondary = await capture_other_expander(
            target.bus, SECONDARY_EXPANDER_ADDRESS
        )
        if abs(final_drive.position_mm - EXTENDED_AWAY_TARGET_MM) > 1.0:
            raise RuntimeError(
                "100 mm move ended outside target tolerance: "
                f"target={EXTENDED_AWAY_TARGET_MM:.6f}, "
                f"actual={final_drive.position_mm:.6f}"
            )
    except BaseException as exc:
        await timing.stop_after_motion_error(motor, exc)
        raise
    emit(
        "extended_move_complete",
        initial_position_mm=baseline_drive.position_mm,
        final_position_mm=final_drive.position_mm,
        target_mm=EXTENDED_AWAY_TARGET_MM,
        primary_initial=asdict(baseline_primary),
        primary_final=asdict(final_primary),
        primary_changed_pins=changed_pins(baseline_primary, final_primary),
        secondary_initial=asdict(baseline_secondary),
        secondary_final=asdict(final_secondary),
        secondary_changed_pins=changed_pins(baseline_secondary, final_secondary),
        persisted=False,
        homes=0,
    )
    return 0


async def run_homeward_scan(
    args: argparse.Namespace,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    target: ExpanderTarget,
) -> int:
    baseline_drive = await require_extended_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(baseline_drive, expected_delays=EXPECTED_DELAYS)
    require_homeward_scan_start(baseline_drive.position_mm)
    baseline_primary = await capture_stable_gpio(gpio_device)
    baseline_secondary = await capture_other_expander(
        target.bus, SECONDARY_EXPANDER_ADDRESS
    )
    emit(
        "homeward_scan_preflight",
        drive=asdict(baseline_drive),
        primary_gpio=asdict(baseline_primary),
        secondary_gpio=asdict(baseline_secondary),
        targets_mm=HOMEWARD_TARGETS_MM,
        rpm=MOVE_RPM,
        accel=MOVE_ACCEL,
    )
    await timing.arm_countdown(args.command, args.arm_delay_s)

    final_preflight = await require_extended_stationary_healthy_y(
        controller, motor_params
    )
    timing.require_live_profile(final_preflight, expected_delays=EXPECTED_DELAYS)
    require_homeward_scan_start(final_preflight.position_mm)
    timing.require_no_position_change(
        baseline_drive,
        final_preflight,
        context="homeward scan arm delay",
    )

    motor = Motor(controller, motor_params)
    motor._cur_pos = final_preflight.position_mm
    motor._last_raw_position = final_preflight.raw_position
    transition_drive: timing.DriveSnapshot | None = None
    transition_primary: GpioSnapshot | None = None
    transition_secondary: GpioSnapshot | None = None
    final_drive = final_preflight
    final_primary = baseline_primary
    final_secondary = baseline_secondary
    try:
        await motor.move_config()
        configured_drive = await require_extended_stationary_healthy_y(
            controller, motor_params
        )
        timing.require_live_profile(configured_drive, expected_delays=EXPECTED_DELAYS)
        timing.require_no_position_change(
            final_preflight,
            configured_drive,
            context="homeward scan motion configuration",
        )
        for target_mm in HOMEWARD_TARGETS_MM:
            final_drive, final_primary = await move_homeward_and_capture(
                motor,
                controller,
                motor_params,
                gpio_device,
                target_mm=target_mm,
            )
            final_secondary = await capture_other_expander(
                target.bus, SECONDARY_EXPANDER_ADDRESS
            )
            emit(
                "homeward_scan_sample",
                target_mm=target_mm,
                primary_gpio=asdict(final_primary),
                secondary_gpio=asdict(final_secondary),
                primary_changed_pins=changed_pins(baseline_primary, final_primary),
                secondary_changed_pins=changed_pins(
                    baseline_secondary, final_secondary
                ),
            )
            if final_primary.y_min_high != baseline_primary.y_min_high:
                transition_drive = final_drive
                transition_primary = final_primary
                transition_secondary = final_secondary
                break
    except BaseException as exc:
        await timing.stop_after_motion_error(motor, exc)
        raise

    emit(
        "homeward_scan_complete",
        initial_position_mm=baseline_drive.position_mm,
        final_position_mm=final_drive.position_mm,
        transition_detected=transition_drive is not None,
        transition_position_mm=(
            transition_drive.position_mm if transition_drive is not None else None
        ),
        primary_initial=asdict(baseline_primary),
        primary_final=asdict(transition_primary or final_primary),
        secondary_initial=asdict(baseline_secondary),
        secondary_final=asdict(transition_secondary or final_secondary),
        commanded_below_zero=False,
        persisted=False,
        homes=0,
    )
    return 0


async def run_confirmed_limit_scan(
    args: argparse.Namespace,
    controller: timing.SharedModbusController,
    motor_params: Any,
    gpio_device: PCA9535,
    target: ExpanderTarget,
    logical_y_min_input: Any,
    logical_y_max_input: Any,
) -> int:
    baseline_drive = await require_extended_stationary_healthy_y(controller, motor_params)
    timing.require_live_profile(baseline_drive, expected_delays=EXPECTED_DELAYS)
    require_scan_start(baseline_drive.position_mm)
    baseline_gpio = await capture_stable_gpio(gpio_device)
    baseline_logical = await capture_stable_logical_limits(
        logical_y_min_input, logical_y_max_input
    )
    emit(
        "confirmed_limit_scan_preflight",
        drive=asdict(baseline_drive),
        gpio=asdict(baseline_gpio),
        application_gpio=asdict(baseline_logical),
        expander=asdict(target),
        away_target_mm=EXTENDED_AWAY_TARGET_MM,
        return_targets_mm=HOMEWARD_TARGETS_MM,
        rpm=MOVE_RPM,
        accel=MOVE_ACCEL,
    )
    await timing.arm_countdown(args.command, args.arm_delay_s)

    final_preflight = await require_extended_stationary_healthy_y(
        controller, motor_params
    )
    timing.require_live_profile(final_preflight, expected_delays=EXPECTED_DELAYS)
    require_scan_start(final_preflight.position_mm)
    timing.require_no_position_change(
        baseline_drive,
        final_preflight,
        context="confirmed limit scan arm delay",
    )
    final_preflight_gpio = await capture_stable_gpio(gpio_device)
    final_preflight_logical = await capture_stable_logical_limits(
        logical_y_min_input, logical_y_max_input
    )
    if (
        final_preflight_gpio.port0 != baseline_gpio.port0
        or final_preflight_gpio.port1 != baseline_gpio.port1
    ):
        raise RuntimeError("GPIO inputs changed during the arm delay; refusing motion")
    if (
        final_preflight_logical.y_min != baseline_logical.y_min
        or final_preflight_logical.y_max != baseline_logical.y_max
    ):
        raise RuntimeError(
            "application GPIO limits changed during the arm delay; refusing motion"
        )

    motor = Motor(controller, motor_params)
    motor._cur_pos = final_preflight.position_mm
    motor._last_raw_position = final_preflight.raw_position
    transition_drive: timing.DriveSnapshot | None = None
    transition_gpio: GpioSnapshot | None = None
    transition_logical: LogicalLimitSnapshot | None = None
    final_drive = final_preflight
    final_gpio = baseline_gpio
    final_logical = baseline_logical
    try:
        await motor.move_config()
        configured_drive = await require_extended_stationary_healthy_y(
            controller, motor_params
        )
        timing.require_live_profile(configured_drive, expected_delays=EXPECTED_DELAYS)
        timing.require_no_position_change(
            final_preflight,
            configured_drive,
            context="confirmed limit scan motion configuration",
        )
        away_drive, away_gpio = await move_extended_and_capture(
            motor,
            controller,
            motor_params,
            gpio_device,
        )
        away_logical = await capture_stable_logical_limits(
            logical_y_min_input, logical_y_max_input
        )
        emit(
            "confirmed_limit_away_sample",
            drive=asdict(away_drive),
            gpio=asdict(away_gpio),
            application_gpio=asdict(away_logical),
            changed_pins=changed_pins(baseline_gpio, away_gpio),
        )
        raw_min_cleared = away_gpio.y_min_high != baseline_gpio.y_min_high
        logical_min_cleared = away_logical.y_min != baseline_logical.y_min

        for target_mm in HOMEWARD_TARGETS_MM:
            final_drive, final_gpio = await move_homeward_and_capture(
                motor,
                controller,
                motor_params,
                gpio_device,
                target_mm=target_mm,
            )
            final_logical = await capture_stable_logical_limits(
                logical_y_min_input, logical_y_max_input
            )
            emit(
                "confirmed_limit_return_sample",
                target_mm=target_mm,
                gpio=asdict(final_gpio),
                application_gpio=asdict(final_logical),
                changed_from_home_pins=changed_pins(baseline_gpio, final_gpio),
                changed_from_away_pins=changed_pins(away_gpio, final_gpio),
            )
            raw_reasserted = (
                raw_min_cleared
                and final_gpio.y_min_high == baseline_gpio.y_min_high
            )
            logical_reasserted = (
                logical_min_cleared and final_logical.y_min == baseline_logical.y_min
            )
            if raw_reasserted or logical_reasserted:
                transition_drive = final_drive
                transition_gpio = final_gpio
                transition_logical = final_logical
                break
    except BaseException as exc:
        await timing.stop_after_motion_error(motor, exc)
        raise

    emit(
        "confirmed_limit_scan_complete",
        initial_position_mm=baseline_drive.position_mm,
        away_position_mm=away_drive.position_mm,
        final_position_mm=final_drive.position_mm,
        y_min_cleared_at_100=raw_min_cleared,
        application_y_min_cleared_at_100=logical_min_cleared,
        y_min_reasserted=transition_drive is not None,
        y_min_reassert_position_mm=(
            transition_drive.position_mm if transition_drive is not None else None
        ),
        y_max_changed_at_100=(away_gpio.y_max_high != baseline_gpio.y_max_high),
        application_y_max_changed_at_100=(
            away_logical.y_max != baseline_logical.y_max
        ),
        gpio_at_home=asdict(baseline_gpio),
        gpio_at_100=asdict(away_gpio),
        gpio_final=asdict(transition_gpio or final_gpio),
        application_gpio_at_home=asdict(baseline_logical),
        application_gpio_at_100=asdict(away_logical),
        application_gpio_final=asdict(transition_logical or final_logical),
        commanded_below_zero=False,
        persisted=False,
        homes=0,
    )
    return 0


async def async_main(args: argparse.Namespace) -> int:
    validate_args(args)
    with timing.acquire_ownership_lock(OWNERSHIP_LOCK_PATH):
        timing.refuse_if_api_is_live(timing.NORMAL_API_HOST, timing.NORMAL_API_PORT)
        motor_params = timing.load_y_motor_params(args.config)
        expander_target = load_expander_target(args.config)
        controller = timing.make_controller(args)
        await controller.connect()
        gpio_device: PCA9535 | None = None
        application_gpio_manager: GpioManager | None = None
        try:
            gpio_device = PCA9535(
                bus_num=expander_target.bus,
                address=expander_target.address,
            )
            logical_y_min_input = None
            logical_y_max_input = None
            if args.command == "scan-confirmed-limits":
                application_gpio_manager = load_application_gpio_manager(args.config)
                logical_y_min_input = application_gpio_manager.input("y_lim_min")
                logical_y_max_input = application_gpio_manager.input("y_lim_max")
            if args.command == "inspect":
                return await run_inspect(
                    controller,
                    motor_params,
                    gpio_device,
                    expander_target,
                )
            if args.command == "move-away-100":
                return await run_extended_move(
                    args,
                    controller,
                    motor_params,
                    gpio_device,
                    expander_target,
                )
            if args.command == "scan-toward-home":
                return await run_homeward_scan(
                    args,
                    controller,
                    motor_params,
                    gpio_device,
                    expander_target,
                )
            if args.command == "scan-confirmed-limits":
                return await run_confirmed_limit_scan(
                    args,
                    controller,
                    motor_params,
                    gpio_device,
                    expander_target,
                    logical_y_min_input,
                    logical_y_max_input,
                )
            return await run_scan(
                args,
                controller,
                motor_params,
                gpio_device,
                expander_target,
            )
        finally:
            try:
                await controller.close()
            finally:
                try:
                    if application_gpio_manager is not None:
                        application_gpio_manager.close()
                finally:
                    if gpio_device is not None:
                        gpio_device.close()


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
