from src.hardware.device_registers import (csv_to_registers,
  get_classes_in_module, DeviceRegisters)
import src.hardware.iclrs_register_types as iclrs_register_types
import pytest

# Create DeviceRegister for the Icl-RS motor controller.
registers_filename = '../src/hardware/device_docs/icl-rs/ICL42.csv'
registers = csv_to_registers(registers_filename)
types = get_classes_in_module(iclrs_register_types)
iclrs_registers = DeviceRegisters(registers, types)

address_name_tuples = [(register.address, register.name) for register in registers]

@pytest.mark.parametrize("address, name", address_name_tuples)
def test_get_register_by_address(address, name):
  register = iclrs_registers.get_register_by_address(address)
  assert register.name == name

@pytest.mark.parametrize("address, name", address_name_tuples)
def test_get_register_by_name(address, name):
  register = iclrs_registers.get_register_by_name(name)
  assert register.address == address

print(iclrs_registers.to_json_dict(0x0001, 250))
print(iclrs_registers.to_json_dict(0x0003, 2))
print(iclrs_registers.to_json_dict(0x0007, 1))
print(iclrs_registers.to_json_dict(0x000F, 1))
print(iclrs_registers.to_json_dict(0x0145, 0b0000000010100110))
print(iclrs_registers.to_json_dict(0x0179, 0b0101010))
print(iclrs_registers.to_json_dict(0x017B, 0x101))
print(iclrs_registers.to_json_dict(0x01BD, 6))
print(iclrs_registers.to_json_dict(0x1801, 0x2211))
print(iclrs_registers.to_json_dict(0x1901, 0x5555))

print(iclrs_registers.to_json_dict(0x1003, 0b111111))

print(iclrs_registers.to_json_dict(0x2203, 0x01))
print(iclrs_registers.to_json_dict(0x2203, 0x02))
print(iclrs_registers.to_json_dict(0x2203, 0x40))
print(iclrs_registers.to_json_dict(0x2203, 0x80))
print(iclrs_registers.to_json_dict(0x2203, 0x100))
print(iclrs_registers.to_json_dict(0x2203, 0x200))


print(iclrs_registers.to_json_dict(0x6200, 0b100000001000001))
print(iclrs_registers.to_json_dict(0x6200, 0b100000101110010))
print(iclrs_registers.to_json_dict(0x6200, 0b000111100100100))
print(iclrs_registers.to_json_dict(0x6200, 0b000010101011000))
