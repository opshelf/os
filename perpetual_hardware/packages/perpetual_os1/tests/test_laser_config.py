import copy

import pytest

from perpetual_os1.common import ConfigError, RootConfig, assert_config


MINIMAL_CONFIG_DICT = {
    "name": "test",
    "version": "0.1.0",
    "robot": {
        "tool": {
            "parameters": {
                "IDLE_POSITION": [0, 0],
                "PICKUP_POSITION_L": [0, 0],
                "PICKUP_POSITION_H": [0, 0],
                "SCAN_POSITION": [0, 0],
                "STAGE_EXTENSION_IO_BOX": 0,
                "STAGE_NUDGE": 0,
                "STAGE_EXTENSION_SHELF_FRONT": 0,
                "STAGE_EXTENSION_SHELF_BACK": 0,
                "Z_EXTENSION_PICK": 0,
                "Z_EXTENSION_PLACE": 0,
                "Z_EXTENSION_IO": 0,
                "Z_MOVE_RPM": 1,
                "Z_MOVE_ACCEL": 1,
                "ROTATION_FRONT": 0,
                "ROTATION_BACK": 0,
                "JOG_X": 0,
                "JOG_Y": 0,
                "PXMM": 1,
                "PYMM": 1,
            },
            "motors": [],
            "poe_camera": {
                "ip": "127.0.0.1",
                "port": 80,
                "url": "",
                "H_ref_size": [1920, 1080],
                "pattern_size": [9, 6],
                "square_size_mm": 25.0,
            },
        },
        "grid": {"path": "storage.json"},
        "io": {
            "motors": [],
            "usb_camera": {
                "usb_index": 0,
                "usb_fourcc": "MJPG",
                "usb_exposure": 25,
                "square_mm": 7.5,
                "fx_px": 708,
                "fy_px": 708,
                "px_per_mm": 1.2,
                "confidence_min": 0,
                "headless_fallback_path": "./fallback.jpg",
            },
            "tof_camera": {
                "csi_port": 0,
                "resolution": [240, 180],
                "fps": 30,
                "max_distance_mm": 2000,
                "confidence_min": 0.9,
            },
        },
    },
    "auth": {"require_auth": False},
}


def test_laser_defaults_to_disabled():
    config = RootConfig.model_validate(copy.deepcopy(MINIMAL_CONFIG_DICT))

    assert config.robot.tool.laser.enabled is False
    assert config.robot.tool.laser.device_id == 1
    assert config.robot.tool.laser.read_function_code == 3
    assert config.robot.tool.laser.register_address == 0
    assert config.robot.tool.laser.register_count == 2
    assert config.robot.tool.laser.distance_register_index == 1
    assert config.robot.tool.laser.distance_scale_mm == pytest.approx(0.001)


def test_assert_config_requires_laser_bus_when_laser_enabled():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["laser"] = {"enabled": True}
    config = RootConfig.model_validate(data)

    with pytest.raises(ConfigError, match="robot.bus is required when robot.tool.laser.enabled is True"):
        assert_config(config)


def test_assert_config_accepts_enabled_laser_with_bus():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["laser"] = {"enabled": True}
    data["robot"]["bus"] = {
        "motor": {
            "ports": ["/dev/ttyUSB1"],
            "baudrate": 115200,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
        "scale": {
            "ports": ["/dev/ttyUSB0"],
            "baudrate": 9600,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
        "laser": {
            "ports": ["/dev/serial/by-id/laser"],
            "baudrate": 9600,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
    }
    config = RootConfig.model_validate(data)

    assert_config(config)


def test_motor_home_sensor_config_is_optional_and_parses():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["motors"] = [
        {
            "motor_name": "x_axis",
            "motor_id": 5,
            "Ip": 10,
            "rpm": 100,
            "dr": 0,
            "mpr": 80,
            "accel": 100,
            "ret_rpm": 50,
            "lim": 1000,
            "home_sensor": {
                "pin": 25,
                "poll_interval_s": 0.02,
                "release_backoff_mm": 5.0,
                "precision_rpm": 25,
            },
        }
    ]

    config = RootConfig.model_validate(data)

    motor = config.robot.tool.motors.by_name("x_axis")
    assert motor.home_sensor is not None
    assert motor.home_sensor.pin == 25
    assert motor.home_sensor.inverted is False
