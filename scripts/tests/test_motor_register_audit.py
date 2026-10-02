from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

AUDIT_PATH = Path(__file__).parents[1] / "motor_register_audit.py"


def load_audit() -> ModuleType:
	spec = importlib.util.spec_from_file_location("motor_register_audit_test", AUDIT_PATH)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


def y_params() -> SimpleNamespace:
	return SimpleNamespace(
		motor_name="y_axis", motor_id=2, Ip=60, rpm=60, dr=0, accel=100, lim=300.0,
		brake_output_function=0x24,
	)


def x_params() -> SimpleNamespace:
	return SimpleNamespace(motor_name="x_axis", motor_id=1, Ip=30, rpm=120, dr=1, accel=100, lim=950.0)


class FakeDrive:
	"""Register file that fails the test on any write."""

	def __init__(self, registers: dict[int, int]) -> None:
		self.registers = registers

	async def read_holding_register_block(self, unit_id: int, address: int, count: int = 1, **_: Any) -> tuple[int, ...]:
		return tuple(self.registers.get(address + i, 0) for i in range(count))

	async def write_single_register(self, *args: Any) -> None:
		raise AssertionError(f"audit wrote to the drive: {args}")


def healthy_drive(audit: ModuleType, params: SimpleNamespace) -> FakeDrive:
	registers = dict(asyncio.run(audit.intended_registers(params)))
	registers[audit.STATUS] = 0x0030  # disabled, cmd and path done
	return FakeDrive(registers)


def test_y_intended_values_come_from_configure() -> None:
	audit = load_audit()
	intended = asyncio.run(audit.intended_registers(y_params()))

	assert intended[audit.SI1] == 0x0080
	assert intended[audit.SO2] == 0x0024
	assert intended[audit.PR0_07] == 0
	assert intended[0x0191] == 60
	assert audit.CONTROL_WORD not in intended


def test_unparked_axis_without_brake_has_no_enable_or_brake_intent() -> None:
	audit = load_audit()
	intended = asyncio.run(audit.intended_registers(x_params()))

	assert audit.PR0_07 not in intended
	assert audit.SI1 not in intended
	assert intended[0x0191] == 30


def test_healthy_drive_is_clean_and_never_written() -> None:
	audit = load_audit()
	report = asyncio.run(audit.audit_motor(healthy_drive(audit, y_params()), y_params()))

	assert report["mismatches"] == 0
	assert report["findings"] == []


def test_di1_still_live_after_si1_change_reports_power_cycle() -> None:
	audit = load_audit()
	drive = healthy_drive(audit, y_params())
	drive.registers[audit.STATUS] = 0x0032  # ENABLED despite SI1 inert and Pr0.07=0

	report = asyncio.run(audit.audit_motor(drive, y_params()))

	assert report["mismatches"] == 0
	assert any("pwr_out0" in f for f in report["findings"])


def test_runtime_registers_differ_without_counting_as_mismatch() -> None:
	audit = load_audit()
	drive = healthy_drive(audit, y_params())
	drive.registers[0x6203] = 999  # velocity left by the last move
	drive.registers[0x0145] = 0x0088  # SI1 back to factory ENABLE N.C.

	report = asyncio.run(audit.audit_motor(drive, y_params()))
	status = {r["register"]: r["status"] for r in report["rows"]}

	assert status["0x6203"] == "RUNTIME"
	assert status["0x0145"] == "MISMATCH"
	assert report["mismatches"] == 1
	assert any("boot repair" in f for f in report["findings"])
	assert "ENABLE N.C. 10ms" in audit.render(report)
