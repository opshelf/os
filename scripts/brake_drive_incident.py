#!/usr/bin/env python3
# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import socket
import sys
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from numbers import Real
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

from hardware.controller import SharedModbusController
from hardware.motor import Motor, NOOP_POSITION_TOLERANCE_MM
from openshelf_os.common import load_config_file


DEFAULT_PORT = os.getenv("OPENSHELF_MOTOR_PORT", "/dev/openshelf-rs485-ch1")
API_LIVE_CHECK_TIMEOUT_SECONDS = 0.2
NORMAL_API_HOST = "127.0.0.1"
NORMAL_API_PORT = 8000
OWNERSHIP_LOCK_PATH = REPO_ROOT / "openshelf_hardware/data/.brake-drive-incident.lock"
WRITE_CONFIRMATION = "SUPERVISED-ISOLATED-Y-TEST"
PERSIST_CONFIRMATION = "PERSIST-CONFIRMED-CS-M234120B-MS30-PROFILE"
RAW_TRIGGER_OUTPUT_FUNCTION = 0xA1
SUPPORTED_REGISTER_MAP = "cs2b-clrs-pr"
Y_AXIS_UNIT = 2
SAVE_CONTROL_REGISTER = 0x1801
SAVE_PARAMETERS_COMMAND = 0x2211
SAVE_STATUS_REGISTER = 0x1901
SAVE_SUCCEEDED = 0x5555
SAVE_FAILED = 0xAAAA
MOTION_RUNNING_BIT = 2
TEST_Y_MIN_MM = 0.0
TEST_Y_MAX_MM = 100.0
TEST_Y_MM_PER_REVOLUTION = 40.0
TEST_Y_COMMAND_PULSES_PER_REVOLUTION = 10000
PERSISTED_Y_PROFILE = {
	"control_mode": 2,
	"command_pulses_per_revolution": 10000,
	"encoder_resolution_cpr": 4000,
	"peak_current_setting": 60,
	"brake_release_delay_ms": 250,
	"brake_engagement_delay_ms": 250,
	"brake_engagement_speed_threshold": 10,
	"so2_function": 0x24,
}

REGISTERS = {
	"command_pulses_per_revolution": (0x0001, 1),
	"control_mode": (0x0003, 1),
	"max_following_error": (0x000B, 1),
	"status": (0x1003, 1),
	"alarm": (0x2203, 1),
	"position_block": (0x602C, 2),
	"position_high": (0x602C, 1),
	"position_low": (0x602D, 1),
	"so2_function": (0x0159, 1),
	"brake_release_delay_ms": (0x0167, 1),
	"brake_engagement_delay_ms": (0x0169, 1),
	"brake_engagement_speed_threshold": (0x016B, 1),
	"output_state": (0x017B, 1),
	"bus_voltage": (0x0177, 1),
	"peak_current_setting": (0x0191, 1),
	"encoder_resolution_cpr": (0x0233, 1),
	"version": (0x01FF, 1),
	"firmware": (0x0201, 1),
}


def auto_int(value: str) -> int:
	return int(value, 0)


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


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Exclusive diagnostic harness for the lb4a-1 Y brake/drive incident."
	)
	parser.add_argument("--port", default=DEFAULT_PORT)
	parser.add_argument("--baudrate", type=int, default=115200)
	parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
	parser.add_argument("--timeout", type=float, default=2.0)
	subparsers = parser.add_subparsers(dest="command", required=True)

	def require_register_map(command_parser: argparse.ArgumentParser) -> None:
		command_parser.add_argument(
			"--register-map",
			choices=[SUPPORTED_REGISTER_MAP],
			required=True,
			help=(
				"Explicitly declare that the installed drive was independently "
				"identified as using the engineer-confirmed CS2B/CLRS-compatible "
				"Pr/register map."
			),
		)

	matrix = subparsers.add_parser("matrix", help="Read-only raw-register soak.")
	require_register_map(matrix)
	matrix.add_argument("--units", nargs="+", type=int, default=[2, 3])
	matrix.add_argument("--iterations", type=int, default=100)
	matrix.add_argument("--interval-ms", type=float, default=10.0)

	reproduce = subparsers.add_parser(
		"same-position-trigger",
		help="Guarded raw path trigger using the drive's exact current position.",
	)
	require_register_map(reproduce)
	reproduce.add_argument("--unit", type=int, default=2)
	reproduce.add_argument("--expect-output-function", type=auto_int, required=True)
	reproduce.add_argument("--cycles", type=int, default=1)
	reproduce.add_argument("--samples", type=int, default=30)
	reproduce.add_argument("--sample-interval-ms", type=float, default=20.0)
	reproduce.add_argument("--confirm", required=True)

	application = subparsers.add_parser(
		"application-noop",
		help="Use the checked-out Motor implementation and prove a same-position move is skipped.",
	)
	require_register_map(application)
	application.add_argument("--config", type=Path, required=True)
	application.add_argument("--motor-name", default="y_axis")

	persist = subparsers.add_parser(
		"persist-profile",
		help=(
			"After transient validation, save the exact confirmed Y profile to drive EEPROM."
		),
	)
	require_register_map(persist)
	persist.add_argument("--confirm", required=True)
	persist.add_argument("--save-timeout-s", type=float, default=5.0)

	return parser


def validate_args(args: argparse.Namespace) -> None:
	if args.timeout <= 0:
		raise ValueError("--timeout must be positive")
	if getattr(args, "register_map", None) != SUPPORTED_REGISTER_MAP:
		raise ValueError(
			"the incident harness is CS2B/CLRS-map-specific; independently identify "
			f"the installed drive, then pass --register-map {SUPPORTED_REGISTER_MAP}"
		)

	if args.command == "matrix":
		if args.iterations <= 0 or args.interval_ms < 0:
			raise ValueError("matrix iterations must be positive and interval non-negative")
		if not args.units:
			raise ValueError("matrix requires at least one unit")
	elif args.command == "same-position-trigger":
		if args.unit != Y_AXIS_UNIT:
			raise ValueError(f"same-position-trigger is restricted to Y unit {Y_AXIS_UNIT}")
		if args.expect_output_function != RAW_TRIGGER_OUTPUT_FUNCTION:
			raise ValueError(
			"same-position-trigger only reproduces the legacy path-completed "
			f"SO2 function 0x{RAW_TRIGGER_OUTPUT_FUNCTION:02X}; "
			"use application-noop to validate the configured brake function"
		)
		if args.confirm != WRITE_CONFIRMATION:
			raise ValueError(
				f"write confirmation mismatch; pass --confirm {WRITE_CONFIRMATION} only during the supervised test"
			)
		if args.cycles != 1:
			raise ValueError("same-position-trigger requires exactly one cycle")
		if args.samples <= 0 or args.sample_interval_ms < 0:
			raise ValueError("samples must be positive and sample interval non-negative")
	elif args.command == "application-noop":
		if args.motor_name != "y_axis":
			raise ValueError("application-noop is restricted to motor-name y_axis")
	elif args.command == "persist-profile":
		if args.confirm != PERSIST_CONFIRMATION:
			raise ValueError(
				"persistence confirmation mismatch; pass "
				f"--confirm {PERSIST_CONFIRMATION} only after transient profile validation"
			)
		if args.save_timeout_s <= 0:
			raise ValueError("--save-timeout-s must be positive")


def make_controller(args: argparse.Namespace) -> SharedModbusController:
	return SharedModbusController(
		port=args.port,
		baudrate=args.baudrate,
		parity=args.parity,
		timeout=args.timeout,
	)


def refuse_if_api_is_live(host: str, port: int) -> None:
	"""Refuse serial ownership when the normal API accepts TCP connections."""
	try:
		connection = socket.create_connection(
			(host, port),
			timeout=API_LIVE_CHECK_TIMEOUT_SECONDS,
		)
	except OSError:
		return
	else:
		connection.close()
		raise RuntimeError(
			f"normal API appears to be listening on {host}:{port}; "
			"stop it with the repo-managed scripts before running this harness"
		)


@contextmanager
def acquire_ownership_lock(lock_path: Path) -> Iterator[None]:
	"""Hold an exclusive nonblocking lock in the host-mounted data directory."""
	if not lock_path.parent.is_dir():
		raise RuntimeError(
			f"ownership lock parent directory does not exist: {lock_path.parent}"
		)

	try:
		lock_file = lock_path.open("a+")
	except OSError as exc:
		raise RuntimeError(f"cannot open ownership lock {lock_path}: {exc}") from exc

	try:
		try:
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError as exc:
			raise RuntimeError(
				f"another brake-drive incident harness already holds {lock_path}"
			) from exc
		except OSError as exc:
			raise RuntimeError(f"cannot acquire ownership lock {lock_path}: {exc}") from exc

		try:
			yield
		finally:
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
	finally:
		lock_file.close()


async def read_register(
	controller: SharedModbusController,
	unit: int,
	name: str,
) -> tuple[int, ...]:
	address, count = REGISTERS[name]
	return await controller.read_holding_register_block(unit, address, count=count)


def decode_y_position_mm(position: tuple[int, ...], command_ppr: int) -> float:
	if len(position) != 2:
		raise RuntimeError(f"expected two Y position registers, received {len(position)}")
	if command_ppr != TEST_Y_COMMAND_PULSES_PER_REVOLUTION:
		raise RuntimeError(
			"refusing Y test-window conversion with unexpected command pulses/revolution: "
			f"expected={TEST_Y_COMMAND_PULSES_PER_REVOLUTION}, actual={command_ppr}"
		)

	raw_position = ((position[0] & 0xFFFF) << 16) | (position[1] & 0xFFFF)
	if raw_position & 0x8000_0000:
		raw_position -= 0x1_0000_0000

	return (
		float(raw_position)
		/ float(command_ppr)
		* TEST_Y_MM_PER_REVOLUTION
	)


async def require_y_in_test_window(
	controller: SharedModbusController,
) -> tuple[tuple[int, ...], float]:
	position = await read_register(controller, Y_AXIS_UNIT, "position_block")
	command_ppr = (
		await read_register(controller, Y_AXIS_UNIT, "command_pulses_per_revolution")
	)[0]
	position_mm = decode_y_position_mm(position, command_ppr)

	if not TEST_Y_MIN_MM <= position_mm <= TEST_Y_MAX_MM:
		raise RuntimeError(
			"refusing Y test outside the supervised 0-100 mm window: "
			f"position_mm={position_mm:.6f}"
		)

	return position, position_mm


async def run_matrix(args: argparse.Namespace, controller: SharedModbusController) -> int:
	counts: Counter[tuple[int, str, str]] = Counter()
	latencies: dict[tuple[int, str], list[float]] = {}

	for iteration in range(args.iterations):
		for unit in args.units:
			for register_name in REGISTERS:
				started = time.monotonic()
				try:
					values = await read_register(controller, unit, register_name)
					latency_ms = (time.monotonic() - started) * 1000.0
					counts[(unit, register_name, "ok")] += 1
					latencies.setdefault((unit, register_name), []).append(latency_ms)
					emit(
						"matrix_read",
						iteration=iteration,
						unit=unit,
						register=register_name,
						values=list(values),
						latency_ms=round(latency_ms, 3),
					)
				except Exception as exc:
					counts[(unit, register_name, "error")] += 1
					emit(
						"matrix_error",
						iteration=iteration,
						unit=unit,
						register=register_name,
						error=str(exc),
					)

		if args.interval_ms:
			await asyncio.sleep(args.interval_ms / 1000.0)

	had_errors = False
	for unit in args.units:
		for register_name in REGISTERS:
			ok = counts[(unit, register_name, "ok")]
			errors = counts[(unit, register_name, "error")]
			had_errors = had_errors or errors > 0
			values = latencies.get((unit, register_name), [])
			emit(
				"matrix_summary",
				unit=unit,
				register=register_name,
				ok=ok,
				errors=errors,
				latency_max_ms=None if not values else round(max(values), 3),
				latency_mean_ms=None if not values else round(sum(values) / len(values), 3),
			)

	return 1 if had_errors else 0


async def sample_trigger_state(
	args: argparse.Namespace,
	controller: SharedModbusController,
	cycle: int,
	expected_position: tuple[int, ...],
) -> list[int]:
	output_states: list[int] = []

	for sample in range(args.samples):
		output_state = (await read_register(controller, args.unit, "output_state"))[0]
		status = (await read_register(controller, args.unit, "status"))[0]
		alarm = (await read_register(controller, args.unit, "alarm"))[0]
		position = await read_register(controller, args.unit, "position_block")
		output_states.append(output_state)
		emit(
			"trigger_sample",
			cycle=cycle,
			sample=sample,
			unit=args.unit,
			output_state=output_state,
			status=status,
			alarm=alarm,
			position=list(position),
		)
		if alarm != 0:
			raise RuntimeError(
				f"Y alarm became nonzero during reproduction: 0x{alarm:04X}"
			)
		if position != expected_position:
			raise RuntimeError(
				"Y encoder position changed during same-position reproduction: "
				f"expected={list(expected_position)}, actual={list(position)}"
			)
		if args.sample_interval_ms:
			await asyncio.sleep(args.sample_interval_ms / 1000.0)

	return output_states


async def run_same_position_trigger(
	args: argparse.Namespace,
	controller: SharedModbusController,
) -> int:
	status_before, alarm_before, initial_position, initial_position_mm = (
		await require_stationary_healthy_y(controller)
	)
	so2_function = (await read_register(controller, args.unit, "so2_function"))[0]
	if so2_function != args.expect_output_function:
		raise RuntimeError(
			f"unit {args.unit} SO2 is 0x{so2_function:02X}, expected "
			f"0x{args.expect_output_function:02X}; refusing to trigger"
		)

	initial_output = (await read_register(controller, args.unit, "output_state"))[0]
	emit(
		"trigger_preflight",
		unit=args.unit,
		so2_function=so2_function,
		position=list(initial_position),
		position_mm=initial_position_mm,
		output_state=initial_output,
		status=status_before,
		alarm=alarm_before,
	)

	all_output_states = [initial_output]
	try:
		for cycle in range(args.cycles):
			position, position_mm = await require_y_in_test_window(controller)
			if position != initial_position:
				raise RuntimeError(
					"Y encoder position changed before same-position reproduction: "
					f"initial={list(initial_position)}, actual={list(position)}"
				)
			await controller.write_single_register(args.unit, 0x6201, position[0])
			await controller.write_single_register(args.unit, 0x6202, position[1])
			await controller.write_single_register(args.unit, 0x6002, 0x10)
			emit(
				"same_position_triggered",
				cycle=cycle,
				unit=args.unit,
				position=list(position),
				position_mm=position_mm,
			)
			all_output_states.extend(
				await sample_trigger_state(
					args,
					controller,
					cycle,
					initial_position,
				)
			)
			await controller.write_single_register(args.unit, 0x6002, 0x40)
	finally:
		try:
			await controller.write_single_register(args.unit, 0x6002, 0x40)
		except Exception as exc:
			emit("final_stop_error", unit=args.unit, error=str(exc))

	final_position = await read_register(controller, args.unit, "position_block")
	final_alarm = (await read_register(controller, args.unit, "alarm"))[0]
	unique_outputs = sorted(set(all_output_states))
	emit(
		"trigger_summary",
		unit=args.unit,
		initial_position=list(initial_position),
		final_position=list(final_position),
		unique_output_states=unique_outputs,
		output_transition_observed=len(unique_outputs) > 1,
		alarm=final_alarm,
	)
	if final_position != initial_position:
		raise RuntimeError(
			"Y encoder position changed during same-position reproduction: "
			f"initial={list(initial_position)}, final={list(final_position)}"
		)
	if final_alarm != 0:
		raise RuntimeError(
			f"Y alarm became nonzero after reproduction: 0x{final_alarm:04X}"
		)
	return 0


async def run_application_noop(
	args: argparse.Namespace,
	controller: SharedModbusController,
) -> int:
	config = load_config_file(args.config)
	motor_params = next(
		(motor for motor in config.robot.tool.motors if motor.motor_name == args.motor_name),
		None,
	)
	if motor_params is None or motor_params.enabled is False:
		raise RuntimeError(f"enabled motor {args.motor_name!r} not found in {args.config}")
	if motor_params.motor_id != Y_AXIS_UNIT:
		raise RuntimeError(
			f"configured y_axis uses unit {motor_params.motor_id}, expected {Y_AXIS_UNIT}"
		)

	motor = Motor(controller, motor_params)
	diagnostics_before = await motor.get_brake_output_diagnostics()
	position_before = await motor.get_position()
	validate_application_noop_position(motor, motor_params, position_before)
	try:
		did_start = await motor.move_to_position_no_wait(position_before)
	except BaseException as movement_error:
		propagation_error = await stop_and_clear_before_propagating(
			motor,
			movement_error,
		)
		if propagation_error is not movement_error:
			raise propagation_error
		raise
	if did_start is not False:
		unexpected_trigger_error = RuntimeError(
			"same-position application move unexpectedly emitted a path command"
		)
		propagation_error = await stop_and_clear_before_propagating(
			motor,
			unexpected_trigger_error,
			include_cleanup_error_in_message=True,
		)
		raise propagation_error

	diagnostics_after = await motor.get_brake_output_diagnostics()
	position_after = await motor.get_position()

	emit(
		"application_noop_summary",
		motor_name=args.motor_name,
		unit=motor_params.motor_id,
		position_before=position_before,
		position_after=position_after,
		did_start=did_start,
		diagnostics_before=diagnostics_before,
		diagnostics_after=diagnostics_after,
	)

	if diagnostics_before["register_function"] != diagnostics_after["register_function"]:
		raise RuntimeError("SO2 function changed during application no-op test")

	return 0


async def read_y_profile(controller: SharedModbusController) -> dict[str, int]:
	return {
		name: (await read_register(controller, Y_AXIS_UNIT, name))[0]
		for name in PERSISTED_Y_PROFILE
	}


def require_exact_y_profile(profile: dict[str, int]) -> None:
	mismatches = {
		name: {"expected": expected, "actual": profile.get(name)}
		for name, expected in PERSISTED_Y_PROFILE.items()
		if profile.get(name) != expected
	}
	if mismatches:
		raise RuntimeError(
			"refusing to persist Y profile with mismatched registers: "
			+ json.dumps(mismatches, sort_keys=True)
		)


async def require_stationary_healthy_y(
	controller: SharedModbusController,
) -> tuple[int, int, tuple[int, ...], float]:
	status = (await read_register(controller, Y_AXIS_UNIT, "status"))[0]
	alarm = (await read_register(controller, Y_AXIS_UNIT, "alarm"))[0]
	position_before, position_before_mm = await require_y_in_test_window(controller)
	await asyncio.sleep(0.1)
	position_after, position_after_mm = await require_y_in_test_window(controller)

	if status & (1 << MOTION_RUNNING_BIT):
		raise RuntimeError(f"refusing Y test while status is running: 0x{status:04X}")
	if alarm != 0:
		raise RuntimeError(f"refusing Y test with alarm: 0x{alarm:04X}")
	if position_before != position_after:
		raise RuntimeError(
			"refusing Y test while position is changing: "
			f"before={list(position_before)}, after={list(position_after)}"
		)

	return status, alarm, position_after, position_after_mm


async def wait_for_save_status(
	controller: SharedModbusController,
	timeout_s: float,
) -> int:
	deadline = time.monotonic() + timeout_s
	while time.monotonic() < deadline:
		value = (
			await controller.read_holding_register_block(
				Y_AXIS_UNIT,
				SAVE_STATUS_REGISTER,
				count=1,
			)
		)[0]
		if value == SAVE_SUCCEEDED:
			return value
		if value == SAVE_FAILED:
			raise RuntimeError("Y drive reported EEPROM save failure (0xAAAA)")
		await asyncio.sleep(0.05)

	raise TimeoutError(f"Y drive EEPROM save did not complete within {timeout_s}s")


async def run_persist_profile(
	args: argparse.Namespace,
	controller: SharedModbusController,
) -> int:
	profile_before = await read_y_profile(controller)
	require_exact_y_profile(profile_before)
	status_before, alarm_before, position_before, position_before_mm = (
		await require_stationary_healthy_y(controller)
	)
	emit(
		"persist_profile_preflight",
		unit=Y_AXIS_UNIT,
		profile=profile_before,
		status=status_before,
		alarm=alarm_before,
		position=list(position_before),
		position_mm=position_before_mm,
	)

	await controller.write_single_register(
		Y_AXIS_UNIT,
		SAVE_CONTROL_REGISTER,
		SAVE_PARAMETERS_COMMAND,
	)
	save_status = await wait_for_save_status(controller, args.save_timeout_s)

	profile_after = await read_y_profile(controller)
	require_exact_y_profile(profile_after)
	status_after, alarm_after, position_after, position_after_mm = (
		await require_stationary_healthy_y(controller)
	)
	emit(
		"persist_profile_summary",
		unit=Y_AXIS_UNIT,
		profile_before=profile_before,
		profile_after=profile_after,
		save_status=save_status,
		status=status_after,
		alarm=alarm_after,
		position=list(position_after),
		position_mm=position_after_mm,
	)
	return 0


async def stop_and_clear_before_propagating(
	motor: Motor,
	original_error: BaseException,
	*,
	include_cleanup_error_in_message: bool = False,
) -> BaseException:
	"""Finish emergency cleanup before returning the error that must propagate."""
	cleanup_task = asyncio.create_task(motor._stop_and_clear_motion())
	cancellation_error: asyncio.CancelledError | None = None

	while not cleanup_task.done():
		try:
			await asyncio.shield(cleanup_task)
		except asyncio.CancelledError as exc:
			if asyncio.current_task().cancelling() > 0:
				cancellation_error = cancellation_error or exc
		except BaseException:
			if cleanup_task.done() is False:
				raise

	try:
		cleanup_task.result()
	except BaseException as cleanup_error:
		propagation_error = cancellation_error or original_error
		propagation_error.add_note(
			"emergency stop-and-clear after application movement failure failed: "
			f"{cleanup_error!r}"
		)
		setattr(propagation_error, "cleanup_error", cleanup_error)
		if include_cleanup_error_in_message and cancellation_error is None:
			original_error.args = (
				f"{original_error}; emergency stop-and-clear failed: {cleanup_error}",
			)
			original_error.__cause__ = cleanup_error

	return cancellation_error or original_error


def validate_application_noop_position(
	motor: Motor,
	motor_params: Any,
	position: float,
) -> None:
	"""Fail closed unless this read is safe to use as a no-op move target."""
	cached_position = motor._cur_pos
	raw_position = motor._last_raw_position

	if raw_position is None:
		raise RuntimeError(
			"application no-op preflight rejected position without a fresh raw-backed read"
		)
	if cached_position is None:
		raise RuntimeError(
			"application no-op preflight rejected position without a cached logical position"
		)

	values = (position, cached_position, raw_position)
	if any(
		not isinstance(value, Real)
		or isinstance(value, bool)
		or not math.isfinite(float(value))
		for value in values
	):
		raise RuntimeError("application no-op preflight rejected a non-finite position read")

	if abs(float(position) - float(cached_position)) > NOOP_POSITION_TOLERANCE_MM:
		raise RuntimeError(
			"application no-op preflight rejected position that does not match its cached logical position"
		)

	try:
		lower_lim = float(getattr(motor_params, "lower_lim", 0))
		upper_lim = float(motor_params.lim)
	except (TypeError, ValueError) as exc:
		raise RuntimeError("application no-op preflight rejected invalid motor limits") from exc

	if (
		not math.isfinite(lower_lim)
		or not math.isfinite(upper_lim)
		or lower_lim > upper_lim
		or not lower_lim <= float(position) <= upper_lim
	):
		raise RuntimeError(
			"application no-op preflight rejected position outside configured motor limits"
		)
	if not TEST_Y_MIN_MM <= float(position) <= TEST_Y_MAX_MM:
		raise RuntimeError(
			"application no-op preflight rejected position outside the supervised "
			"0-100 mm Y test window"
		)


async def async_main(args: argparse.Namespace) -> int:
	validate_args(args)
	with acquire_ownership_lock(OWNERSHIP_LOCK_PATH):
		refuse_if_api_is_live(NORMAL_API_HOST, NORMAL_API_PORT)
		controller = make_controller(args)
		await controller.connect()
		try:
			if args.command == "matrix":
				return await run_matrix(args, controller)
			if args.command == "same-position-trigger":
				return await run_same_position_trigger(args, controller)
			if args.command == "persist-profile":
				return await run_persist_profile(args, controller)
			return await run_application_noop(args, controller)
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
