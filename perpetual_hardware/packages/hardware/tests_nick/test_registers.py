import importlib.util
import json
import sys
from pathlib import Path

import hardware.drivers.iclrs_register_types as iclrs_register_types
import pytest
from hardware.drivers.iclrs import IclrsRegister
from hardware.drivers.registers import (
    DeviceRegisters,
    registers_model_factory,
)
from pydantic import ValidationError
from test_data import register_types

REGISTERS_CSV_FILENAME = "registers.csv"
TYPES_MODULE = "register_types.py"
CONFIG_JSON_FILENAME = "test_config.json"


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


# @pytest.fixture(scope="module")
# def register_types_module(test_path):
#     register_types_module_path = (
#         test_path / "test_data" / TYPES_MODULE
#     )
#     # Dynamically load the module from the specified file path
#     spec = importlib.util.spec_from_file_location(
#         "register_types", register_types_module_path
#     )
#     register_types_module = importlib.util.module_from_spec(spec)
#     sys.modules["register_types"] = register_types_module
#     spec.loader.exec_module(register_types_module)
#     return register_types_module


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


@pytest.fixture(scope="session")
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
    types = set(
        DeviceRegisters.get_classes_in_module(register_types)
    )
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


# # Create DeviceRegister for the Icl-RS motor controller.
# types = DeviceRegisters.get_classes_in_module(iclrs_register_types)
# registers_filename = '../src/hardware/device_docs/icl-rs/ICL42.csv'
# registers = DeviceRegisters.csv_to_registers(registers_filename, types)
# iclrs_registers = DeviceRegisters(registers)

# # --- Tests ---
# address_name_tuples = [(register.address, register.name) for register in registers]
# @pytest.mark.parametrize("address, name", address_name_tuples)
# def test_get_register_by_address(address, name):
#   register = iclrs_registers.get_register_by_address(address)
#   assert register.name == name

# @pytest.mark.parametrize("address, name", address_name_tuples)
# def test_get_register_by_name(address, name):
#   register = iclrs_registers.get_register_by_name(name)
#   assert register.address == address

# # Load sample config JSON
# config_json_filename = './test_data/config_hardware_a.json'
# try:
#   with open(config_json_filename, 'r') as file:
#     data = json.load(file)
#     print(data)
# except FileNotFoundError:
#     print(f"Error: The file {config_json_filename} was not found.")
# except json.JSONDecodeError as e:
#     print(f"Failed to decode JSON: {e}")
# config_dict = data['motors'][0]["initial_register_values"]
# name_field_tuples = [(k,v) for k,v in config_dict.items()]

# @pytest.mark.parametrize("name, field", name_field_tuples)
# def test_encode_decode(name, field):
#   address_value_tuple = iclrs_registers.encode(name, field)
#   name_field_dict = iclrs_registers.decode(*address_value_tuple)
#   assert name_field_dict == {name.lower(): field}

# # # Load sample config JSON
# # config_json_filename = './data/invalid_register_values.json'
# # try:
# #   with open(config_json_filename, 'r') as file:
# #     config_dict = json.load(file)
# # except FileNotFoundError:
# #     print("Error: The file 'data.json' was not found.")
# # except json.JSONDecodeError as e:
# #     print(f"Failed to decode JSON: {e}")
# # name_field_tuples = [(k,v) for k,v in config_dict.items()]

# # @pytest.mark.parametrize("name, field", name_field_tuples)
# # def test_encode_invalid_values(name, field):
# #   exception_caught = False
# #   try:
# #     address_value_tuple = iclrs_registers.encode(name, field)
# #   except Exception as e:
# #      print(e)
# #      exception_caught = True
# #   assert exception_caught

# @pytest.mark.parametrize("code", [0x01, 0x02, 0x40, 0x80, 0x100, 0x200])
# def test_readable_alarms(code):
#   print(IclrsRegister(address = 0x2203, value = code).message)
#   assert True

# @pytest.mark.parametrize("code", [0x5555, 0xAAAA])
# def test_readable_control_word_status(code):
#   print(IclrsRegister(address = 0x1901, value = code).message)
#   assert True

# print(iclrs_registers.decode(0x0001, 250))
# print(iclrs_registers.decode(0x0003, 2))
# print(iclrs_registers.decode(0x0007, 1))
# print(iclrs_registers.decode(0x000F, 1))
# print(iclrs_registers.decode(0x0145, 0b0000000010100110))
# print(iclrs_registers.decode(0x0159, 0x20))
# print(iclrs_registers.decode(0x0179, 0b0101010))
# print(iclrs_registers.decode(0x017B, 0x101))
# print(iclrs_registers.decode(0x01BD, 6))
# print(iclrs_registers.decode(0x1801, 0x2211))
# print(iclrs_registers.decode(0x1901, 0x5555))
# print(iclrs_registers.decode(0x1003, 0b111111))
# print(iclrs_registers.decode(0x6200, 0b100000001000001))
# print(iclrs_registers.decode(0x6200, 0b100000101110010))
# print(iclrs_registers.decode(0x6200, 0b000111100100011))
