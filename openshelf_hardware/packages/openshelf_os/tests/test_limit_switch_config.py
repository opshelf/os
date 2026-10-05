"""Machines are built with and without their axis limit switches fitted."""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional
from unittest.mock import AsyncMock

import pytest

import openshelf_os.common as c
from hardware.exceptions import MotorLimitError
from hardware.motor import Motor
from openshelf_os.tool import Tool
from openshelf_os.tool import _create_motor_if_enabled as create_tool_motor

TOOL_PARAMETERS = {
    "IDLE_POSITION": [0, 0],
    "PICKUP_POSITION_L": [0, 0],
    "PICKUP_POSITION_H": [0, 0],
    "SCAN_POSITION": [0, 0],
    "STAGE_EXTENSION_IO_BOX": 1,
    "STAGE_NUDGE": 1,
    "STAGE_EXTENSION_SHELF_FRONT": 1,
    "STAGE_EXTENSION_SHELF_BACK": 1,
    "Z_EXTENSION_PICK": 1,
    "Z_EXTENSION_PLACE": 1,
    "Z_EXTENSION_IO": 1,
    "Z_MOVE_RPM": 1,
    "Z_MOVE_ACCEL": 1,
    "ROTATION_FRONT": 0,
    "ROTATION_BACK": 0,
}

DEFAULT_LIMIT_PINS = (
    "x_lim_min",
    "y_lim_min",
    "z_lim_min",
    "s_lim_min",
    "r_lim_min",
    "io_lim_min",
    "x_lim_minunsafe",
    "x_lim_maxunsafe",
)


class LimitInput:
    """A switch that never engages, which is what an unfitted input reads."""

    def __init__(self, name: str) -> None:
        self.name = name

    def read(self) -> bool:
        return False


class FakeGpio:
    def __init__(self, pins: Iterable[str] = DEFAULT_LIMIT_PINS) -> None:
        self.pins = set(pins)
        self.requested: List[str] = []

    def has_pin(self, name: str) -> bool:
        return name in self.pins

    def input(self, name: str) -> LimitInput:
        self.requested.append(name)
        return LimitInput(name)


def motor_entry(name: str, motor_id: int, **overrides: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "motor_name": name,
        "motor_id": motor_id,
        "Ip": 30,
        "rpm": 100,
        "dr": 0,
        "mpr": 1.0,
        "accel": 100,
        "ret_rpm": 100,
        "lim": 100,
    }
    entry.update(overrides)
    return entry


def tool_params(motors: List[Dict[str, Any]]) -> c.Tool:
    return c.Tool.model_validate({"parameters": TOOL_PARAMETERS, "motors": motors})


def make_tool(motors: List[Dict[str, Any]], gpio: Optional[FakeGpio] = None) -> Tool:
    return Tool(
        AsyncMock(),
        AsyncMock(),
        tool_params(motors),
        safety=None,
        gpio=gpio if gpio is not None else FakeGpio(),
    )


def test_tool_skips_the_home_limit_switch_of_a_motor_without_one(
    caplog: pytest.LogCaptureFixture,
) -> None:
    motors = [
        motor_entry("stage", 3, limit_switches_enabled=False),
        motor_entry("x_axis", 5),
    ]
    gpio = FakeGpio()
    caplog.set_level(logging.INFO, logger="openshelf_os.tool")

    tool = make_tool(motors, gpio)

    assert tool._ms._home_limit_input is None
    assert isinstance(tool._mx._home_limit_input, LimitInput)
    assert gpio.requested == ["x_lim_min", "x_lim_minunsafe", "x_lim_maxunsafe"]
    assert "Motor 'stage' limit switches disabled via config" in caplog.text
    assert "s_lim_min" in caplog.text


def test_tool_keeps_the_clearance_switches_when_x_is_absent_from_config() -> None:
    gpio = FakeGpio()

    tool = make_tool([motor_entry("stage", 3)], gpio)

    assert tool._mx is None
    assert isinstance(tool._x_lim_minunsafe_input, LimitInput)
    assert isinstance(tool._x_lim_maxunsafe_input, LimitInput)


@pytest.mark.asyncio
async def test_homing_a_motor_without_switches_verifies_no_limit_input() -> None:
    motors = c.Motors.model_validate([motor_entry("stage", 3, limit_switches_enabled=False)])

    motor = create_tool_motor(
        AsyncMock(), motors, "stage", None, logging.getLogger("test"), FakeGpio(), home_limit_pin="s_lim_min"
    )

    assert motor._home_limit_input is None
    await motor._verify_home_limit_engaged()


@pytest.mark.asyncio
async def test_homing_a_motor_with_a_switch_still_requires_it_to_engage() -> None:
    motors = c.Motors.model_validate([motor_entry("stage", 3)])
    gpio = FakeGpio(pins=["s_lim_min"])

    motor = create_tool_motor(
        AsyncMock(), motors, "stage", None, logging.getLogger("test"), gpio, home_limit_pin="s_lim_min"
    )

    assert isinstance(motor, Motor)
    with pytest.raises(MotorLimitError, match="s_lim_min"):
        await motor._verify_home_limit_engaged()
