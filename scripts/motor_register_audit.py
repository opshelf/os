#!/usr/bin/env python3
"""Read-only audit of a Leadshine CL2B drive: intended vs actual registers.

Intended values are recorded by running ``Motor.configure()`` against an
in-memory controller, so they cannot drift from the code. Nothing is written
to the drive, saved or moved. Run with the API stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import y_brake_timing_diagnostic as base  # noqa: E402
from hardware import motor_helpers  # noqa: E402
from hardware.motor import ENABLE_INPUT_INERT, PARKED_AXES, Motor  # noqa: E402
from openshelf_os.common import load_and_validate_config  # noqa: E402

CONTROL_WORD = 0x1801
SAVE_STATUS = 0x1901
SAVE_OK = 0x5555
CONTROL_MODE = 0x0003  # one-word reads return a malformed payload on lb4a-2
PR0_07 = 0x000F
SI1 = 0x0145
SO2 = 0x0159
STATUS = 0x1003
ALARM = 0x2203
DI_STATE = 0x0179
DO_STATE = 0x017B
BUS_VOLTAGE = 0x0177
DIP = 0x0187

INPUT_REGS = range(0x0145, 0x0152, 2)
OUTPUT_REGS = range(0x0157, 0x015C, 2)
# Rewritten by every move or home, so a difference at idle is expected.
RUNTIME_REGS = frozenset({0x0007, 0x6200, 0x6201, 0x6202, 0x6203, 0x6204, 0x6205})
CONTEXT_REGS = (PR0_07, *INPUT_REGS, *OUTPUT_REGS)
STATUS_REGS = (STATUS, ALARM, DI_STATE, DO_STATE, BUS_VOLTAGE, DIP)
HEX_REGS = frozenset({*INPUT_REGS, *OUTPUT_REGS, 0x6000, 0x6200, STATUS, ALARM, DI_STATE, DO_STATE, DIP})

INPUT_FUNCS = {
    0x00: "INVALID", 0x07: "ALM-CLR", 0x08: "ENABLE", 0x19: "SEL-DEF", 0x20: "CTRG",
    0x21: "HOME", 0x22: "STP", 0x23: "PJOG", 0x24: "NJOG", 0x25: "POT", 0x26: "NOT",
    0x27: "ORG", 0x28: "ADD0", 0x29: "ADD1", 0x2A: "ADD2", 0x2B: "ADD3", 0x2C: "JOG2",
}
INPUT_FILTER_MS = (10, 1, 2, 3, 4, 5, 6, 8, 15, 20, 30, 40, 50, 100, 200, 500)
OUTPUT_FUNCS = {
    0x00: "INVALID", 0x05: "SEL-DEF", 0x20: "CMD_OK", 0x21: "MC_OK",
    0x22: "HOME_OK", 0x23: "INP", 0x24: "BRAKE", 0x25: "ALM",
}
BRAKE_FUNCS = frozenset({0x24, 0xA4})
ENABLE_FUNCTION = 0x08
STATUS_BITS = {0: "FAULT", 1: "ENABLED", 2: "RUNNING", 4: "CMD_DONE", 5: "PATH_DONE", 6: "HOME_DONE"}
ALARMS = {
    0x01: "overcurrent", 0x02: "overvoltage", 0x08: "encoder disconnected",
    0x09: "duplicate input function", 0x20: "out of tolerance", 0x40: "current sampling",
    0x80: "shaft lock / phase loss", 0x100: "parameter self-tuning", 0x200: "EEPROM",
}
REGISTER_INFO = {reg: (param, name, default) for reg, param, name, _d, _r, default, _u in motor_helpers.motor_registers}


class RecordingController:
    """In-memory drive for a configure() dry run: records writes, echoes reads."""

    def __init__(self) -> None:
        self.registers: dict[int, int] = {}

    async def write_single_register(self, unit_id: int, address: int, value: int) -> None:
        if address != CONTROL_WORD:
            self.registers[address] = int(value)

    async def read_holding_register_block(self, unit_id: int, address: int, count: int = 1, **_: Any) -> tuple[int, ...]:
        if address == SAVE_STATUS:
            return (SAVE_OK,) * count
        return tuple(self.registers.get(address + i, 0) for i in range(count))

    async def read_holding_registers(self, unit_id: int, address: int, count: int = 1) -> int:
        return (await self.read_holding_register_block(unit_id, address, count))[0]


async def intended_registers(params: Any) -> dict[int, int]:
    """Final value configure() leaves in each register it writes."""
    controller = RecordingController()
    logging.disable(logging.CRITICAL)
    try:
        await Motor(controller, params).configure()  # type: ignore[arg-type]
    finally:
        logging.disable(logging.NOTSET)
    return controller.registers


async def read_register(controller: Any, unit: int, address: int) -> int:
    count = 2 if address == CONTROL_MODE else 1
    return int((await controller.read_holding_register_block(unit, address, count=count))[0])


def decode(address: int, value: int | None) -> str:
    if value is None:
        return ""
    if address in INPUT_REGS:
        func = INPUT_FUNCS.get(value & 0x7F, f"?0x{value & 0x7F:02X}")
        filt = INPUT_FILTER_MS[(value >> 8) & 0xF]
        return f"{func} {'N.C.' if value & 0x80 else 'N.O.'} {filt}ms"
    if address in OUTPUT_REGS:
        func = OUTPUT_FUNCS.get(value & 0x7F, f"?0x{value & 0x7F:02X}")
        return f"{func}{' inverted' if value & 0x80 else ''}"
    if address == STATUS:
        return " ".join(name for bit, name in STATUS_BITS.items() if value >> bit & 1) or "-"
    if address == ALARM:
        return "none" if value == 0 else ALARMS.get(value, "unknown")
    if address == DI_STATE:
        return " ".join(f"DI{i + 1}={value >> i & 1}" for i in range(7))
    if address == DO_STATE:
        return " ".join(f"DO{i + 1}={value >> i & 1}" for i in range(3))
    if address == BUS_VOLTAGE:
        return f"{value / 10:.1f} V"
    if address == PR0_07:
        return "forced enable" if value else "IO enable"
    return ""


def fmt(address: int, value: int | None) -> str:
    if value is None:
        return "-"
    return f"0x{value:04X}" if address in HEX_REGS else str(value)


def findings(params: Any, actual: dict[int, int | None]) -> list[str]:
    """Rules for the brake/park failure modes seen since the 260910 merge."""
    out: list[str] = []
    si1, pr007, so2 = actual.get(SI1), actual.get(PR0_07), actual.get(SO2)
    status, alarm = actual.get(STATUS), actual.get(ALARM)
    enabled = status is not None and bool(status >> 1 & 1)
    running = status is not None and bool(status >> 2 & 1)
    parked_axis = params.motor_name in PARKED_AXES

    if parked_axis and si1 is not None and si1 != ENABLE_INPUT_INERT:
        out.append(f"SI1=0x{si1:04X}, expected 0x{ENABLE_INPUT_INERT:04X}: boot repair has not run or failed.")
    enable_inputs = [
        f"SI{(a - SI1) // 2 + 1}"
        for a in INPUT_REGS
        if actual.get(a) is not None and actual[a] & 0x7F == ENABLE_FUNCTION
    ]
    if parked_axis and enable_inputs:
        out.append(f"{', '.join(enable_inputs)} carries ENABLE: Pr0.07=0 falls back to it, so the axis cannot park.")
    if pr007 == 0 and enabled and not enable_inputs:
        out.append(
            "Pr0.07=0 and no input carries ENABLE, yet the drive reports ENABLED. Either an "
            "input function change is not live yet (needs a drive restart: power-cycle "
            "pwr_out0), or this drive enables itself when no input carries ENABLE."
        )
    if parked_axis and pr007 == 1 and not running:
        out.append("Pr0.07=1 while idle: the axis did not park (fault/cancel/failed home, or no clean move since boot).")
    if getattr(params, "brake_output_function", None) is not None and so2 is not None and so2 not in BRAKE_FUNCS:
        out.append(f"SO2=0x{so2:04X} is not a brake function: the brake will not follow the enable.")
    if alarm:
        out.append(f"Active alarm 0x{alarm:04X} ({ALARMS.get(alarm, 'unknown')}).")
    return out


async def audit_motor(controller: Any, params: Any) -> dict[str, Any]:
    """Compare configure()'s intended registers with the live drive."""
    intended = await intended_registers(params)
    unit = int(params.motor_id)
    addresses = sorted({*intended, *CONTEXT_REGS}) + list(STATUS_REGS)
    actual: dict[int, int | None] = {}
    errors: dict[int, str] = {}
    for address in addresses:
        try:
            actual[address] = await read_register(controller, unit, address)
        except Exception as exc:  # one bad read must not hide the rest
            actual[address] = None
            errors[address] = f"{type(exc).__name__}: {exc}"

    rows = []
    for address in addresses:
        want, got = intended.get(address), actual[address]
        if address in errors:
            state = "ERR"
        elif want is None:
            state = "STATUS" if address in STATUS_REGS else "-"
        elif want == got:
            state = "OK"
        else:
            state = "RUNTIME" if address in RUNTIME_REGS else "MISMATCH"
        param, name, default = REGISTER_INFO.get(address, (None, "", None))
        rows.append({
            "register": f"0x{address:04X}", "param": param or "", "name": name,
            "intended": fmt(address, want), "actual": fmt(address, got),
            "default": "-" if default is None else str(default), "status": state,
            "decode": errors.get(address) or decode(address, got),
        })

    return {
        "motor_name": params.motor_name,
        "motor_id": unit,
        "rows": rows,
        "findings": findings(params, actual),
        "mismatches": sum(r["status"] in ("MISMATCH", "ERR") for r in rows),
    }


def render(report: dict[str, Any]) -> str:
    cols = ("register", "param", "name", "intended", "actual", "default", "status", "decode")
    widths = {c: max(len(c), *(len(str(r[c])) for r in report["rows"])) for c in cols[:-1]}
    line = lambda r: "  ".join(str(r[c]).ljust(widths[c]) for c in cols[:-1]) + "  " + str(r["decode"])  # noqa: E731
    out = [f"== {report['motor_name']} (unit {report['motor_id']}) ==", line({c: c.upper() for c in cols})]
    out += [line(r) for r in report["rows"]]
    out.append("findings:" if report["findings"] else "findings: none")
    out += [f"  - {f}" for f in report["findings"]]
    return "\n".join(out)


def select_motors(config_path: Path, motor: str, audit_all: bool) -> list[Any]:
    config = load_and_validate_config(json.loads(config_path.read_text()), str(config_path))
    motors = [m for m in config.robot.tool.motors if m.enabled and (audit_all or m.motor_name == motor)]
    if not motors:
        raise ValueError(f"no enabled motor {'' if audit_all else repr(motor) + ' '}in {config_path}")
    return motors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--motor", default="y_axis")
    parser.add_argument("--all", action="store_true", help="audit every enabled tool motor")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--port", default=base.DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
    parser.add_argument("--timeout", type=float, default=2.0)
    return parser


async def async_main(args: argparse.Namespace) -> int:
    motors = select_motors(args.config, args.motor, args.all)
    with base.acquire_ownership_lock(base.OWNERSHIP_LOCK_PATH):
        base.refuse_if_api_is_live(base.NORMAL_API_HOST, base.NORMAL_API_PORT)
        controller = base.make_controller(args)
        await controller.connect()
        try:
            reports = [await audit_motor(controller, m) for m in motors]
        finally:
            await controller.close()

    print(json.dumps(reports, indent=2) if args.json else "\n\n".join(map(render, reports)))
    return int(any(r["mismatches"] or r["findings"] for r in reports))


def main() -> int:
    args = build_parser().parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # config, profile (MotorConfigError) or bus errors
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
