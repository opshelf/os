import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.cabinet import Cabinet
from openshelf_os.robot.core import STARTUP_HOME_SKIPPED_CABINET_OPEN, Robot


class Pin:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def read(self) -> bool:
        return self.value

    def write(self, value: bool) -> None:
        self.value = value


def make_robot(top_closed: bool, bottom_closed: bool) -> Robot:
    pins = {
        "cabinet_top_closed": Pin(top_closed),
        "cabinet_bot_closed": Pin(bottom_closed),
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
    robot._logger = logging.getLogger("test_startup_home_cabinet")
    robot._emit_init = lambda *_args, **_kwargs: None
    robot._connect_controller = AsyncMock()
    robot._bus_settings = {"motor": None}
    robot._motorbus = robot._scalebus = object()
    robot._config = SimpleNamespace(get=lambda *_args: SimpleNamespace(enabled=False))
    robot._gpio = gpio
    robot._weight_guard = None
    robot._tool = SimpleNamespace(configure=AsyncMock())
    robot._io = SimpleNamespace(configure=AsyncMock())
    robot._cabinet = Cabinet(gpio=gpio)
    robot._get_motor_by_name = lambda _name: None
    return robot


@pytest.mark.asyncio
async def test_startup_home_is_skipped_while_a_cabinet_door_is_open() -> None:
    # The safety guard only starts with the worker, after configure: open doors
    # must stop the boot home here or every axis homes with the cabinet open.
    robot = make_robot(top_closed=True, bottom_closed=False)

    reason = await robot.configure(isolate=True, home_on_init=True)

    assert reason == STARTUP_HOME_SKIPPED_CABINET_OPEN
    robot._tool.configure.assert_awaited_once_with(isolate=True, home_on_init=False)
    assert robot._io.configure.await_args.kwargs["home_on_init"] is False
    await robot._cabinet.stop()
