import copy

import pytest

from openshelf_os.common import ConfigError, RootConfig, assert_config


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
            },
            "motors": [],
            "poe_camera": {
                "url": "",
            },
        },
        "grid": {"path": "storage.json"},
        "io": {
            "motors": [],
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


def test_motor_rotation_safety_flag_defaults_to_disabled():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["motors"] = [{
        "motor_name": "stage",
        "motor_id": 1,
        "Ip": 1,
        "rpm": 1,
        "dr": 0,
        "mpr": 1,
        "accel": 1,
        "ret_rpm": 1,
        "lim": 10,
    }]
    config = RootConfig.model_validate(data)

    stage_motor = config.robot.tool.motors.by_name("stage")

    assert stage_motor.rotation_safe_stage_extension_enabled is False
    assert stage_motor.rotation_safe_stage_extension_mm == pytest.approx(8.5)


def test_root_config_get_motor_supports_name_and_id_lookup():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["motors"] = [{
        "motor_name": "stage",
        "motor_id": 1,
        "Ip": 1,
        "rpm": 1,
        "dr": 0,
        "mpr": 1,
        "accel": 1,
        "ret_rpm": 1,
        "lim": 10,
    }]
    config = RootConfig.model_validate(data)

    assert config.robot.tool.motors.by_name("stage").motor_id == 1
    assert config.robot.tool.motors.by_id(1).motor_name == "stage"


def test_motors_iterates_over_motor_models():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["motors"] = [{
        "motor_name": "stage",
        "motor_id": 1,
        "Ip": 1,
        "rpm": 1,
        "dr": 0,
        "mpr": 1,
        "accel": 1,
        "ret_rpm": 1,
        "lim": 10,
    }]
    config = RootConfig.model_validate(data)

    assert [motor.motor_name for motor in config.robot.tool.motors] == ["stage"]
    assert any(motor.motor_name == "stage" for motor in config.robot.tool.motors)
    assert len(config.robot.tool.motors) == 1
    assert config.robot.tool.motors[0].motor_name == "stage"


def test_assert_config_requires_laser_bus_when_laser_enabled():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["laser"] = {"enabled": True}
    config = RootConfig.model_validate(data)

    with pytest.raises(ConfigError, match="robot.bus is required when robot.tool.laser.enabled is True"):
        assert_config(config)



def test_bus_port_serializes_canonical_single_port_config():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["bus"] = {
        "motor": {
            "port": "/dev/openshelf-rs485-ch1",
            "baudrate": 115200,
        },
        "scale": {
            "port": "/dev/openshelf-scale",
            "baudrate": 9600,
        },
    }
    config = RootConfig.model_validate(data)

    dumped = config.model_dump(mode="json")
    dumped_motor_bus = dumped["robot"]["bus"]["motor"]
    assert dumped_motor_bus["port"] == "/dev/openshelf-rs485-ch1"
    assert "ports" not in dumped_motor_bus


def test_legacy_bus_ports_coerces_to_canonical_port_config():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["bus"] = {
        "motor": {
            "ports": ["/dev/openshelf-rs485-ch1", "/dev/ttyUSB1"],
            "baudrate": 115200,
        },
        "scale": {
            "ports": ["/dev/openshelf-rs485-ch2", "/dev/ttyUSB0"],
            "baudrate": 9600,
        },
    }
    config = RootConfig.model_validate(data)

    assert config.robot.bus is not None
    assert config.robot.bus.motor.port == "/dev/openshelf-rs485-ch1"
    assert config.robot.bus.motor.ports == ["/dev/openshelf-rs485-ch1", "/dev/ttyUSB1"]
    assert config.robot.bus.scale.port == "/dev/openshelf-rs485-ch2"

    dumped_motor_bus = config.model_dump(mode="json")["robot"]["bus"]["motor"]
    assert dumped_motor_bus["port"] == "/dev/openshelf-rs485-ch1"
    assert "ports" not in dumped_motor_bus


def test_assert_config_accepts_enabled_laser_with_bus():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["laser"] = {"enabled": True}
    data["robot"]["bus"] = {
        "motor": {
            "port": "/dev/ttyUSB1",
            "baudrate": 115200,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
        "scale": {
            "port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
        "laser": {
            "port": "/dev/serial/by-id/laser",
            "baudrate": 9600,
            "parity": "N",
            "timeout": 2.0,
            "retry_delay_s": 0.25,
        },
    }
    config = RootConfig.model_validate(data)

    assert_config(config)
