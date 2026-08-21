from types import SimpleNamespace

import pytest

from openshelf_os.common import ConfigError
from openshelf_os.robot.core import Robot


class FakeGpio:
    def __init__(self, values: dict[str, bool]) -> None:
        self._values = values

    def has_pin(self, name: str) -> bool:
        return name in self._values

    def input(self, name: str) -> SimpleNamespace:
        return SimpleNamespace(read=lambda: self._values[name])


def test_y_axis_limit_state_uses_logical_gpio_inputs() -> None:
    robot = Robot.__new__(Robot)
    robot._gpio = FakeGpio({"y_limit_min": True, "y_limit_max": False})

    assert robot.get_y_axis_limit_state() == {
        "minimum_triggered": True,
        "maximum_triggered": False,
    }


def test_y_axis_limit_state_rejects_missing_input() -> None:
    robot = Robot.__new__(Robot)
    robot._gpio = FakeGpio({"y_limit_min": False})

    with pytest.raises(ConfigError, match="y_limit_max"):
        robot.get_y_axis_limit_state()
