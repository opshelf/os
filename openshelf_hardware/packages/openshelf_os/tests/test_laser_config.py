import copy

import pytest

from openshelf_os.common import RootConfig


MINIMAL_CONFIG_DICT = {
    "name": "test",
    "schema_version": 5,
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


def test_y_axis_accepts_exact_brake_profile():
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["motors"] = [{
        "motor_name": "y_axis",
        "motor_id": 2,
        "Ip": 60,
        "rpm": 1,
        "dr": 0,
        "mpr": 1,
        "accel": 1,
        "ret_rpm": 1,
        "lim": 10,
        "brake_output_function": 0x24,
    }]

    config = RootConfig.model_validate(data)

    y_axis = config.robot.tool.motors.by_name("y_axis")
    assert y_axis.brake_output_function == 0x24
    assert y_axis.brake_release_delay_ms == 250
    assert y_axis.brake_engagement_delay_ms == 250
    assert y_axis.brake_engagement_speed_threshold == 10
    assert y_axis.control_mode == 2
    assert y_axis.ppr == 10000
    assert y_axis.encoder_resolution_cpr == 4000


@pytest.mark.parametrize("function", [0xA0, 0xA1, 0x21])
def test_motor_rejects_unsupported_brake_output_function(function: int):
    """0xA1 was inverted path-complete: a status signal, not a brake output."""
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    data["robot"]["tool"]["motors"] = [{
        "motor_name": "y_axis",
        "motor_id": 2,
        "Ip": 60,
        "rpm": 1,
        "dr": 0,
        "mpr": 1,
        "accel": 1,
        "ret_rpm": 1,
        "lim": 10,
        "brake_output_function": function,
    }]

    with pytest.raises(ValueError, match="brake_output_function"):
        RootConfig.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Ip", 59),
        ("ppr", 4000),
        ("encoder_resolution_cpr", 1000),
        ("control_mode", 0),
        ("brake_output_function", 0xA4),
        ("brake_release_delay_ms", 249),
        ("brake_engagement_delay_ms", 249),
        ("brake_engagement_speed_threshold", 9),
    ],
)
def test_y_axis_rejects_controller_profile_mismatch(field: str, value: int):
    data = copy.deepcopy(MINIMAL_CONFIG_DICT)
    motor = {
        "motor_name": "y_axis",
        "motor_id": 2,
        "Ip": 60,
        "rpm": 1,
        "dr": 0,
        "mpr": 1,
        "accel": 1,
        "ret_rpm": 1,
        "lim": 10,
        "brake_output_function": 0x24,
        field: value,
    }
    data["robot"]["tool"]["motors"] = [motor]

    with pytest.raises(ValueError, match=field):
        RootConfig.model_validate(data)


