from openshelf_os.config.models import GpioConfig, Robot


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


def test_robot_gpio_empty_object_expands_to_defaults() -> None:
    config = make_robot_config()
    config["gpio"] = {}

    robot = Robot.model_validate(config)

    assert isinstance(robot.gpio, GpioConfig)
    assert "light_curtain_1" in robot.gpio.pins
    assert "light_curtain_2" in robot.gpio.pins
    assert robot.gpio.pins["light_curtain_1"].pin == 15
    assert robot.gpio.pins["light_curtain_2"].pin == 14
    assert "cabinet_top_closed" in robot.gpio.pins
    assert "cabinet_bot_closed" in robot.gpio.pins
    assert robot.gpio.pins["cabinet_top_closed"].inverted is False
    assert robot.gpio.pins["cabinet_bot_closed"].inverted is False
