import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hardware.curtain import MotionSafetyInterlock
from openshelf_os.cabinet import Cabinet
from openshelf_os.robot.core import Robot


class Pin:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def read(self) -> bool:
        return self.value

    def write(self, value: bool) -> None:
        self.value = value


def make_robot() -> Robot:
    pins = {
        "estop": Pin(),
        "cabinet_top_closed": Pin(True),
        "cabinet_bot_closed": Pin(True),
        "cabinet_top_unlock": Pin(),
        "cabinet_bot_unlock": Pin(),
    }
    gpio = SimpleNamespace(
        has_pin=lambda name: name in pins,
        input=pins.__getitem__,
        output=pins.__getitem__,
        apply_initial_outputs=lambda: None,
        observe_all_inputs=lambda: None,
    )
    robot = Robot.__new__(Robot)
    robot._logger = logging.getLogger("test_startup_home_safety")
    robot._connect_controller = AsyncMock()
    robot._bus_settings = {"motor": None}
    robot._motorbus = robot._scalebus = object()
    # Scale and laser off; the e-stop stays monitored.
    robot._config = SimpleNamespace(get=lambda key, *_args: SimpleNamespace(enabled=key == "robot.emergency_stop"))
    robot._gpio = gpio
    robot._weight_guard = None
    robot._motion_safety = MotionSafetyInterlock()
    robot._tool = SimpleNamespace(
        configure=AsyncMock(),
        startup_home=AsyncMock(),
        stop=AsyncMock(),
        mark_configured_motors_ready=Mock(),
        is_tray_held=False,
    )
    robot._io = SimpleNamespace(
        configure=AsyncMock(),
        home_door=AsyncMock(),
        stop_door=AsyncMock(),
        mark_door_ready=Mock(),
        curtain_blocked=False,
        door_open=False,
    )
    robot._io.get_curtain_state = lambda: robot._io.curtain_blocked
    robot._io.get_door_state = lambda: robot._io.door_open
    robot._cabinet = Cabinet(gpio=gpio)
    robot._get_motor_by_name = lambda _name: None
    return robot


def press_estop(robot: Robot) -> None:
    robot._gpio.input("estop").value = True


def open_cabinet(robot: Robot) -> None:
    robot._gpio.input("cabinet_bot_closed").value = False


def block_curtain(robot: Robot) -> None:
    robot._io.curtain_blocked = True
    robot._io.door_open = True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trip", "hazard"),
    [
        (press_estop, "the emergency stop was pressed"),
        (open_cabinet, "the cabinet doors were open"),
        (block_curtain, "the light curtain was blocked with the IO door open"),
    ],
)
async def test_startup_home_is_skipped_while_a_safety_input_is_tripped(trip, hazard) -> None:
    # lb4a-2: every axis homed at boot with the e-stop held and the cabinet open,
    # because the worker's guards only start after configure.
    robot = make_robot()
    trip(robot)

    reason = await robot.configure(isolate=True, home_on_init=True)

    assert reason == f"Startup homing skipped because {hazard}"
    robot._tool.startup_home.assert_not_awaited()
    robot._io.home_door.assert_not_awaited()
    assert robot._motion_safety.blocked is True
    await robot._cabinet.stop()


@pytest.mark.asyncio
async def test_estop_pressed_during_startup_home_stops_the_drives_and_abandons_it() -> None:
    robot = make_robot()
    homing = asyncio.Event()

    async def long_home(**_kwargs) -> None:
        homing.set()
        await asyncio.sleep(30)

    robot._tool.startup_home.side_effect = long_home
    configure = asyncio.create_task(robot.configure(isolate=True, home_on_init=True))
    await asyncio.wait_for(homing.wait(), timeout=1)

    press_estop(robot)
    reason = await asyncio.wait_for(configure, timeout=1)

    assert reason == "Startup homing stopped because the emergency stop was pressed"
    assert robot._motion_safety.blocked is True
    robot._tool.stop.assert_awaited_once()
    robot._io.stop_door.assert_awaited_once()
    robot._io.home_door.assert_not_awaited()
    await robot._cabinet.stop()


@pytest.mark.asyncio
async def test_startup_home_runs_and_leaves_motion_released_when_inputs_are_clear() -> None:
    robot = make_robot()

    reason = await robot.configure(isolate=True, home_on_init=True)

    assert reason is None
    robot._tool.startup_home.assert_awaited_once_with(isolate=True)
    robot._io.home_door.assert_awaited_once()
    assert robot._motion_safety.blocked is False
    await robot._cabinet.stop()


@pytest.mark.asyncio
async def test_cleanup_move_of_an_abandoned_startup_home_does_not_park_or_run_later() -> None:
    # The home's cancel unwinds into a cleanup move that waits on the blocked interlock;
    # parked there, configure never returned and the move ran once someone unblocked.
    robot = make_robot()
    homing = asyncio.Event()
    cleanup_moves: list[float] = []

    async def home_with_cleanup_move(**_kwargs) -> None:
        try:
            homing.set()
            await asyncio.sleep(30)
        finally:
            await robot._motion_safety.wait_clear()
            cleanup_moves.append(500.0)

    robot._tool.startup_home.side_effect = home_with_cleanup_move
    configure = asyncio.create_task(robot.configure(isolate=True, home_on_init=True))
    await asyncio.wait_for(homing.wait(), timeout=1)

    press_estop(robot)
    reason = await asyncio.wait_for(configure, timeout=1)
    robot._motion_safety.unblock()
    await asyncio.sleep(0)

    assert reason == "Startup homing stopped because the emergency stop was pressed"
    assert cleanup_moves == []
    await robot._cabinet.stop()
