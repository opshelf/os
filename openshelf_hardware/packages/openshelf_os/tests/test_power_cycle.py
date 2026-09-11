import asyncio
import logging
from types import SimpleNamespace

import pytest

from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec
from hardware.motor import MotorStatus
from openshelf_os.common import ConfigError
from openshelf_os.exceptions import MotorError
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
        _status=status, _is_homed=True, _hard_stopped=False, _cur_pos=12.0
    )


def make_robot(gpio, motors: dict, gripper=None) -> Robot:
    robot = Robot.__new__(Robot)
    robot._gpio = gpio
    robot._logger = logging.getLogger("test")
    robot._get_motor_by_name = motors.get
    robot._tool = SimpleNamespace(_gs=gripper)
    return robot


@pytest.mark.asyncio
async def test_power_cycle_drops_then_restores_the_pin(monkeypatch) -> None:
    gpio = make_gpio()
    output = gpio.output(GANTRY_POWER_PIN)
    motors = {"x_axis": make_motor(), "y_axis": make_motor()}
    robot = make_robot(gpio, motors)

    observed: list[bool] = []

    async def record_sleep(_seconds):
        observed.append(output.read())

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    await robot.power_cycle_rail("pwr_out0")

    assert observed == [True]  # held asserted across the wait
    assert output.read() is False


@pytest.mark.asyncio
async def test_power_cycle_holds_for_three_seconds(monkeypatch) -> None:
    gpio = make_gpio()
    robot = make_robot(gpio, {"x_axis": make_motor(), "y_axis": make_motor()})
    delays: list[float] = []

    async def record_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    await robot.power_cycle_rail("pwr_out0")

    assert delays == [3.0]


@pytest.mark.asyncio
async def test_power_cycle_marks_every_tool_axis_unhomed(monkeypatch) -> None:
    gpio = make_gpio()
    motors = {
        name: make_motor()
        for name in ("x_axis", "y_axis", "z_axis", "stage", "rotate")
    }
    robot = make_robot(gpio, motors)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await robot.power_cycle_rail("pwr_out0")

    for motor in motors.values():
        assert motor._is_homed is False
        assert motor._hard_stopped is True
        assert motor._cur_pos is None


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
    robot = make_robot(gpio, {"x_axis": make_motor(), "y_axis": make_motor()})

    task = asyncio.create_task(robot.power_cycle_rail("pwr_out0"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert output.read() is False


@pytest.mark.asyncio
async def test_power_cycle_requires_the_pin_to_be_configured() -> None:
    gpio = GpioManager({}, fake=True)
    robot = make_robot(gpio, {"x_axis": make_motor(), "y_axis": make_motor()})

    with pytest.raises(ConfigError, match=GANTRY_POWER_PIN):
        await robot.power_cycle_rail("pwr_out0")


@pytest.mark.asyncio
async def test_unknown_rail_is_rejected() -> None:
    robot = make_robot(make_gpio(), {})

    with pytest.raises(ConfigError, match="Unknown power rail"):
        await robot.power_cycle_rail("pwr_out9")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rail", "pin", "expected_axes"),
    [
        ("pwr_out0", 12, ("x_axis", "y_axis", "z_axis", "stage", "rotate")),
        ("pwr_out1", 13, ("door",)),
        ("pwr_out2", 14, ()),
        ("pwr_out3", 15, ()),
    ],
)
async def test_every_rail_cycles_its_own_pin(
    monkeypatch, rail, pin, expected_axes
) -> None:
    gpio = GpioManager(
        {
            rail: GpioPinSpec(
                name=rail,
                board="pca9535",
                expander="pca9535_1",
                pin=pin,
                direction="output",
            )
        },
        fake=True,
    )
    motors = {name: make_motor() for name in expected_axes}
    robot = make_robot(gpio, motors)
    held: list[bool] = []

    async def record_sleep(_seconds):
        held.append(gpio.output(rail).read())

    monkeypatch.setattr(asyncio, "sleep", record_sleep)

    assert await robot.power_cycle_rail(rail) == expected_axes
    assert held == [True]
    assert gpio.output(rail).read() is False


@pytest.mark.asyncio
async def test_power_cycle_forces_the_gripper_to_reconfigure(monkeypatch) -> None:
    """pwr_out0 drops the gripper too, so its cached configuration is stale."""
    gpio = make_gpio()
    gripper = SimpleNamespace(_configured=True)
    robot = make_robot(gpio, {}, gripper=gripper)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await robot.power_cycle_rail("pwr_out0")

    assert gripper._configured is False


@pytest.mark.asyncio
async def test_power_cycle_of_another_rail_leaves_the_gripper_alone(monkeypatch) -> None:
    gpio = GpioManager(
        {
            "pwr_out1": GpioPinSpec(
                name="pwr_out1",
                board="pca9535",
                expander="pca9535_1",
                pin=13,
                direction="output",
            )
        },
        fake=True,
    )
    gripper = SimpleNamespace(_configured=True)
    robot = make_robot(gpio, {"door": make_motor()}, gripper=gripper)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    await robot.power_cycle_rail("pwr_out1")

    assert gripper._configured is True


@pytest.mark.asyncio
async def test_rail_states_invert_the_pin() -> None:
    """Asserted cuts power, so a cleared pin reads as powered."""
    gpio = make_gpio()
    robot = make_robot(gpio, {})

    assert robot.get_power_rail_states()["pwr_out0"] is True

    gpio.output("pwr_out0").write(True)
    assert robot.get_power_rail_states()["pwr_out0"] is False


@pytest.mark.asyncio
async def test_rail_states_are_none_for_unconfigured_pins() -> None:
    robot = make_robot(make_gpio(), {})
    states = robot.get_power_rail_states()

    assert states["pwr_out0"] is True
    assert states["pwr_out1"] is None
    assert set(states) == {"pwr_out0", "pwr_out1", "pwr_out2", "pwr_out3"}
