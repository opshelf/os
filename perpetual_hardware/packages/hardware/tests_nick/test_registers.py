from pathlib import Path
from typing import List

import pytest
from hardware.drivers.registers import (
    DeviceRegisters,
    registers_model_factory,
)
from pydantic import BaseModel, ValidationError
from test_data import register_types

REGISTERS_CSV_FILENAME = "registers.csv"
TYPES_MODULE = "register_types.py"
CONFIG_JSON_FILENAME = "registers_config.json"


# --- Fixtures ---
@pytest.fixture(scope="module")
def test_path(request):
    test_file_path = Path(request.module.__file__)
    parent_path = test_file_path.parent
    return parent_path


@pytest.fixture(scope="module")
def config_json_path(test_path):
    json_file_path = test_path / "test_data" / CONFIG_JSON_FILENAME
    return json_file_path


@pytest.fixture(scope="module")
def registers_csv_path(test_path):
    registers_csv_path = (
        test_path / "test_data" / REGISTERS_CSV_FILENAME
    )
    return registers_csv_path


@pytest.fixture(scope="module")
def registers(registers_csv_path):
    types = DeviceRegisters.get_classes_in_module(register_types)
    registers = DeviceRegisters.csv_to_registers(
        registers_csv_path, types
    )
    return registers


@pytest.fixture(scope="module")
def device_registers(registers):
    return DeviceRegisters(registers)


@pytest.fixture(scope="module")
def registers_model(registers):
    return registers_model_factory(registers)


@pytest.fixture(scope="module")
def config_json_str(config_json_path):
    try:
        with open(config_json_path, "r", encoding="utf-8") as f:
            json_string = f.read()
    except FileNotFoundError:
        print(f"Error: The file '{config_json_path}' was not found.")
    except Exception as e:
        print(f"An error occured: {e}")
    else:
        return json_string


# --- Tests ---
def test_get_classes_in_module():
    types = set(DeviceRegisters.get_classes_in_module(register_types))
    expected_types = set(
        [
            register_types.Color,
            register_types.Direction,
            register_types.MotionSettings,
        ]
    )
    assert types == expected_types


def test_csv_to_registers(registers_csv_path):
    types = DeviceRegisters.get_classes_in_module(register_types)
    try:
        registers = DeviceRegisters.csv_to_registers(
            registers_csv_path, types
        )
        assert True
    except Exception as e:
        print(f"Error: {e}")
        assert False


def test_create_device_registers(registers):
    try:
        device_registers = DeviceRegisters(registers)
        assert True
    except Exception as e:
        print(f"Error: {e}")
        assert False


address_name_tuples = [
    (0x0001, "int register"),
    (0x0002, "bool register"),
    (0x0003, "color"),
    (0x0004, "motion settings"),
]


@pytest.mark.parametrize("address, name", address_name_tuples)
def test_get_register_by_address(address, name, device_registers):
    register = device_registers.get_register_by_address(address)
    assert register.name == name


@pytest.mark.parametrize("address, name", address_name_tuples)
def test_get_register_by_name(address, name, device_registers):
    register = device_registers.get_register_by_name(name)
    assert register.address == address


def test_create_register_model(registers):
    try:
        RegistersModel = registers_model_factory(registers)
        assert True
    except Exception as e:
        print(f"Error: {e}")
        assert False


name_value_tuples = [
    ("int register", 500, 500),
    ("bool register", True, True),
    (
        "color",
        register_types.Color.RED,
        register_types.Color.RED.value,
    ),
    (
        "motion settings",
        {
            "enabled": True,
            "direction": register_types.Direction.RIGHT,
            "speed": 100,
        },
        0b110010011,
    ),
    ("int register", 10000, ValidationError),
    ("bool register", -1, ValidationError),
]


@pytest.mark.parametrize(
    "name, value, expected_result", name_value_tuples
)
def test_register_model(
    name, value, expected_result, registers_model
):
    try:
        register = registers_model(name=name, value=value)
        result = register.modbus[1]
    except ValidationError:
        result = ValidationError
    assert result == expected_result


def test_register_model_validate_json(
    registers_model, config_json_str
):
    class Config(BaseModel):
        registers: List[registers_model]

    try:
        config = Config.model_validate_json(config_json_str)
        assert True
    except ValidationError as e:
        print(f"Error: {e}")
        # Test fail if model fails to validate from JSON.
        assert False
