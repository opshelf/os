import asyncio
import logging
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec
from hardware.motor import MotorStatus
from openshelf_os.exceptions import MotorError
from openshelf_os.robot import core
from openshelf_os.robot.core import Robot

GANTRY_POWER_PIN = "pwr_out0"  # the rail most of these exercise

REAL_SLEEP = asyncio.sleep


async def no_sleep(_seconds) -> None:
    """Skip the hold without recursing into the patched asyncio.sleep."""
    await REAL_SLEEP(0)


def make_gpio() -> GpioManager:
    return GpioManager(
        {
            GANTRY_POWER_PIN: GpioPinSpec(
                name=GANTRY_POWER_PIN,
                board="pca9535",
                expander="pca9535_1",
                pin=12,
                direction="output",
            )
        },
        fake=True,
    )


def make_motor(status: MotorStatus = MotorStatus.STOPPED) -> SimpleNamespace:
    return SimpleNamespace(
        _status=status,
        _is_homed=True,
        _hard_stopped=False,
        _cur_pos=12.0,
        # power_cycle_rail reconfigures each drive once it answers again.
        get_param=AsyncMock(return_value=0),
        configure=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def short_boot_timeout(monkeypatch):
    """no_sleep turns the boot poll into a spin, so never wait the real deadline."""
    monkeypatch.setattr(core, "POWER_CYCLE_BOOT_TIMEOUT_S", 0.05)


def make_robot(gpio, motors: dict, gripper=None) -> Robot:
    robot = Robot.__new__(Robot)
    robot._gpio = gpio
    robot._logger = logging.getLogger("test")
    robot._get_motor_by_name = motors.get
    robot._tool = SimpleNamespace(_gs=gripper)
    return robot


@pytest.mark.asyncio
async def test_power_cycle_refuses_while_an_axis_is_moving() -> None:
    gpio = make_gpio()
    motors = {"x_axis": make_motor(), "y_axis": make_motor(MotorStatus.MOVING)}
    robot = make_robot(gpio, motors)

    with pytest.raises(MotorError, match="y_axis is moving"):
        await robot.power_cycle_rail("pwr_out0")

    assert gpio.output(GANTRY_POWER_PIN).read() is False


@pytest.mark.asyncio
async def test_power_cycle_restores_power_even_if_the_wait_is_cancelled() -> None:
    gpio = make_gpio()
    output = gpio.output(GANTRY_POWER_PIN)
    motors = {"x_axis": make_motor(), "y_axis": make_motor()}
    robot = make_robot(gpio, motors)

    task = asyncio.create_task(robot.power_cycle_rail("pwr_out0"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert output.read() is False
    for motor in motors.values():
        assert motor._is_homed is False
        assert motor._hard_stopped is True
        assert motor._cur_pos is None
        motor.configure.assert_awaited_once()


@pytest.mark.asyncio
async def test_power_cycle_reconfigures_every_axis_it_dropped(monkeypatch) -> None:
    """Homing only restores velocity; accel, current and gains come from configure()."""
    motors = {
        name: make_motor()
        for name in ("x_axis", "y_axis", "z_axis", "stage", "rotate")
    }
    robot = make_robot(make_gpio(), motors)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await robot.power_cycle_rail("pwr_out0")

    for motor in motors.values():
        motor.configure.assert_awaited_once()
        # Reconfiguring must not resurrect the homed flag.
        assert motor._is_homed is False
