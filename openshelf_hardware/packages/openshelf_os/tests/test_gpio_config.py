import logging

import pytest

from openshelf_os.config.models import (
    GPIO_PIN_ALIASES,
    GpioConfig,
    Robot,
    compare_gpio_pins_to_defaults,
)
from openshelf_os.robot.core import Robot as RobotCore
from hardware.gpio.manager import GpioManager


def make_robot_config() -> dict[str, object]:
    parameters = {
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

    return {
        "tool": {
            "parameters": parameters,
            "motors": [],
            "poe_camera": {"url": "rtsp://example.test/camera"},
        },
        "grid": {"path": "storage.json"},
        "io": {"motors": []},
    }


def test_robot_gpio_is_optional_when_not_configured() -> None:
    robot = Robot.model_validate(make_robot_config())

    assert robot.gpio is None


def test_robot_gpio_accepts_legacy_pcf8575_config() -> None:
    config = make_robot_config()
    config["gpio"] = {
        "expanders": [
            {
                "name": "main",
                "type": "pcf8575",
                "bus": 1,
                "address": 0x20,
                "interrupt": {
                    "board": "rpi",
                    "pin": 4,
                    "pull": "up",
                    "edge": "falling",
                },
            }
        ],
        "pins": {
            "light_curtain": {
                "board": "pcf8575",
                "expander": "main",
                "pin": 8,
                "direction": "input",
                "pull": "up",
                "inverted": True,
            }
        },
    }

    robot = Robot.model_validate(config)

    assert robot.gpio is not None
    assert robot.gpio.expanders[0].type == "pcf8575"
    assert robot.gpio.pins["light_curtain"].board == "pcf8575"
    assert robot.gpio.pins["light_curtain"].expander == "main"


def test_robot_gpio_empty_object_expands_to_defaults() -> None:
    config = make_robot_config()
    config["gpio"] = {}

    robot = Robot.model_validate(config)

    assert isinstance(robot.gpio, GpioConfig)
    assert "light_curtain" in robot.gpio.pins
    assert robot.gpio.pins["light_curtain"].pin == 1
    assert robot.gpio.pins["estop"].board == "pca9535"
    assert robot.gpio.pins["estop"].pin == 10
    assert robot.gpio.pins["estop"].pull == "off"
    assert robot.gpio.pins["estop"].inverted is False
    assert robot.gpio.pins["io_lim_max"].pin == 0
    assert robot.gpio.pins["io_lim_min"].pin == 15
    assert robot.gpio.pins["io_1"].pin == 3
    physical_keys = [(pin.board, pin.expander, pin.pin) for pin in robot.gpio.pins.values()]
    assert len(physical_keys) == len(set(physical_keys))
    assert "cabinet_top_closed" in robot.gpio.pins
    assert "cabinet_bot_closed" in robot.gpio.pins
    assert robot.gpio.pins["cabinet_top_closed"].inverted is True
    assert robot.gpio.pins["cabinet_bot_closed"].inverted is True
    assert robot.gpio.pins["cabinet_top_closed"].debounce_ms == 50
    assert robot.gpio.pins["cabinet_bot_closed"].debounce_ms == 50


def power_pin(pin: int) -> dict[str, object]:
    return {"board": "pca9535", "expander": "pca9535_1", "pin": pin, "direction": "output"}


def test_legacy_power_control_pin_is_renamed_to_pwr_out0() -> None:
    """A config declaring pins replaces the defaults, so the old key must migrate."""
    gpio = GpioConfig.model_validate(
        {"pins": {"power_control": power_pin(12), "pwr_out1": power_pin(13)}}
    )

    assert "power_control" not in gpio.pins
    assert gpio.pins["pwr_out0"].pin == 12
    assert gpio.pins["pwr_out1"].pin == 13


@pytest.mark.parametrize("legacy,modern", sorted(GPIO_PIN_ALIASES.items()))
def test_legacy_pin_names_map_to_defaults(legacy: str, modern: str) -> None:
    """Every alias migrates its key and targets a real default pin."""
    spec = GpioConfig().pins[modern].model_dump()
    gpio = GpioConfig.model_validate({"pins": {legacy: spec}})

    assert legacy not in gpio.pins
    assert gpio.pins[modern].pin == spec["pin"]


def test_explicit_pwr_out0_wins_over_the_legacy_alias() -> None:
    raw_pins = {"power_control": power_pin(12), "pwr_out0": power_pin(12)}
    gpio = GpioConfig.model_validate({"pins": raw_pins})

    assert "power_control" not in gpio.pins
    assert gpio.pins["pwr_out0"].pin == 12
    assert GpioManager.from_config(gpio, fake=True).has_pin("pwr_out0")


def test_default_pins_already_use_the_new_name() -> None:
    gpio = GpioConfig()

    assert "pwr_out0" in gpio.pins
    assert "power_control" not in gpio.pins


def test_missing_and_extra_pins_are_reported_against_defaults() -> None:
    missing, unrecognized = compare_gpio_pins_to_defaults(["pwr_out0", "not_a_real_pin"])

    assert "pwr_out1" in missing
    assert "pwr_out0" not in missing
    assert unrecognized == ["not_a_real_pin"]


def test_defaults_report_no_drift() -> None:
    assert compare_gpio_pins_to_defaults(GpioConfig().pins) == ([], [])


def test_robot_warns_when_config_pins_drift_from_defaults(caplog) -> None:
    robot = RobotCore.__new__(RobotCore)
    robot._logger = logging.getLogger("test")
    gpio = GpioConfig.model_validate({"pins": {"pwr_out0": power_pin(12), "mystery": power_pin(1)}})

    with caplog.at_level(logging.WARNING):
        RobotCore._warn_on_gpio_pin_drift(robot, gpio)

    assert "missing defaults" in caplog.text
    assert "not in the defaults" in caplog.text


def test_robot_is_quiet_when_pins_match_defaults(caplog) -> None:
    robot = RobotCore.__new__(RobotCore)
    robot._logger = logging.getLogger("test")

    with caplog.at_level(logging.WARNING):
        RobotCore._warn_on_gpio_pin_drift(robot, GpioConfig())

    assert caplog.text == ""
