import json

import hardware.drivers.iclrs_register_types as iclrs_register_types
import pytest
from hardware.drivers.iclrs import IclrsRegister
from hardware.drivers.registers import DeviceRegisters

REGISTERS_CSV_FILENAME = 'ICL42.csv'
CONFIG_JSON_FILENAME = 'config_hardware_a.json'

# --- Fixtures ---
@pytest.fixture(scope="module")
def test_path(request):
    file_path = request.module.__file__
    module_dir = file_path.parent
    print(file_path)
    print(module_dir)
    return module_dir

@pytest.fixture(scope="module")
def config_json_path(test_path):
    json_file_path = test_path / "test_data" / CONFIG_JSON_FILENAME
    return json_file_path

@pytest.fixture(scope="module")
def registers_csv_path(test_path):
    registers_csv_path = test_path / "test_data" / REGISTERS_CSV_FILENAME
    return registers_csv_path

# Create DeviceRegister for the Icl-RS motor controller.
types = DeviceRegisters.get_classes_in_module(iclrs_register_types)
registers_filename = '../src/hardware/device_docs/icl-rs/ICL42.csv'
registers = DeviceRegisters.csv_to_registers(registers_filename, types)
iclrs_registers = DeviceRegisters(registers)

# --- Tests ---
address_name_tuples = [(register.address, register.name) for register in registers]
@pytest.mark.parametrize("address, name", address_name_tuples)
def test_get_register_by_address(address, name):
  register = iclrs_registers.get_register_by_address(address)
  assert register.name == name

@pytest.mark.parametrize("address, name", address_name_tuples)
def test_get_register_by_name(address, name):
  register = iclrs_registers.get_register_by_name(name)
  assert register.address == address

# Load sample config JSON
config_json_filename = './test_data/config_hardware_a.json'
try:
  with open(config_json_filename, 'r') as file:
    data = json.load(file)
    print(data)
except FileNotFoundError:
    print(f"Error: The file {config_json_filename} was not found.")
except json.JSONDecodeError as e:
    print(f"Failed to decode JSON: {e}")
config_dict = data['motors'][0]["initial_register_values"]
name_field_tuples = [(k,v) for k,v in config_dict.items()]

@pytest.mark.parametrize("name, field", name_field_tuples)
def test_encode_decode(name, field):
  address_value_tuple = iclrs_registers.encode(name, field)
  name_field_dict = iclrs_registers.decode(*address_value_tuple)
  assert name_field_dict == {name.lower(): field}

# # Load sample config JSON
# config_json_filename = './data/invalid_register_values.json'
# try:
#   with open(config_json_filename, 'r') as file:
#     config_dict = json.load(file)
# except FileNotFoundError:
#     print("Error: The file 'data.json' was not found.")
# except json.JSONDecodeError as e:
#     print(f"Failed to decode JSON: {e}")
# name_field_tuples = [(k,v) for k,v in config_dict.items()]

# @pytest.mark.parametrize("name, field", name_field_tuples)
# def test_encode_invalid_values(name, field):
#   exception_caught = False
#   try:
#     address_value_tuple = iclrs_registers.encode(name, field)
#   except Exception as e:
#      print(e)
#      exception_caught = True
#   assert exception_caught

@pytest.mark.parametrize("code", [0x01, 0x02, 0x40, 0x80, 0x100, 0x200])
def test_readable_alarms(code):
  print(IclrsRegister(address = 0x2203, value = code).message)
  assert True

@pytest.mark.parametrize("code", [0x5555, 0xAAAA])
def test_readable_control_word_status(code):
  print(IclrsRegister(address = 0x1901, value = code).message)
  assert True

print(iclrs_registers.decode(0x0001, 250))
print(iclrs_registers.decode(0x0003, 2))
print(iclrs_registers.decode(0x0007, 1))
print(iclrs_registers.decode(0x000F, 1))
print(iclrs_registers.decode(0x0145, 0b0000000010100110))
print(iclrs_registers.decode(0x0159, 0x20))
print(iclrs_registers.decode(0x0179, 0b0101010))
print(iclrs_registers.decode(0x017B, 0x101))
print(iclrs_registers.decode(0x01BD, 6))
print(iclrs_registers.decode(0x1801, 0x2211))
print(iclrs_registers.decode(0x1901, 0x5555))
print(iclrs_registers.decode(0x1003, 0b111111))
print(iclrs_registers.decode(0x6200, 0b100000001000001))
print(iclrs_registers.decode(0x6200, 0b100000101110010))
print(iclrs_registers.decode(0x6200, 0b000111100100011))