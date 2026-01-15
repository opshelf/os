from typing import Any
import csv
from dataclasses import dataclass
from iclrs_parameters import (ControlMode, MotorDirection, 
  DigitalInputFunction, DigitalInputOutputMode, DigitalInputFilter, 
  DigitalInputSettings, DigitalOutputFunction, DigitalOutputSettings, IsEnabled,
  BaudRate, ControlWordFunction, HomingDirection, HomingType, HomingMode, 
  PathOperationMode, PathSettings, PathCoordinate, Alarm, MotionStatus,
  ControlWordStatus)
from enum import Enum

@dataclass
class Register():
  register_address: int
  name: str
  valid_min: int
  valid_max: int
  description: str
  type: str
  

class DeviceRegisters():
  def __init__(self, registers: dict, types: list):
    self.registers = registers
    self.types = types
    self.name_type_mapping = {type.__name__:type for type in types}

    for type in types:
      if issubclass(type, Enum):
        mapping = self._create_map_of_names_to_enum_members(type)
        self.name_type_mapping = self.name_type_mapping | mapping

    print(self.name_type_mapping)

  def _create_map_of_names_to_enum_members(self, enum):
    names = [member.name for member in enum]
    enum_name = enum.__name__
    mapping = {f'{enum_name}.{name}':enum[name] 
             for name in names}
    return mapping

  def get_register_by_name(self, name: str) -> dict:
    found_register = next((d for d in self.registers if d.get('Name').strip().lower() == name.strip().lower()), None)
    return found_register
  
  def get_register_by_address(self, address: int) -> dict:
    found_register = next((d for d in self.registers if int(d.get('Register Address'), 16) == address), None)
    return found_register
  
  def translate_to(self, name: str, value: Any) -> tuple[int, int]:
    address = int(self.get_register_by_name(name)['Register Address'], 16)
    range = self.get_register_by_name(name)['Range']
    min = 0
    max = 0xFFFF   
    if range != '':
      min = int(range.split('-')[0])
      max = int(range.split('-')[1])

    if type(value) == int:
      if not self._is_value_in_range(value, min, max):
        raise ValueError("Value not in the register's valid range")
      return (address, value)
    
    if type(value) == str:
      value = self.name_type_mapping[value]

    if type(value) == dict:
      register_type = self.get_register_by_name(name)['Type']
      MyClass = self.name_type_mapping[register_type]
      for k,v in value.items():
        if isinstance(v, str):
          if v == 'False' or v == 'True':
            value[k] = (v == 'True')
          else:
            value[k] = self.name_type_mapping[v]
      instance = MyClass(**value)
      value = instance.value        
      return (address, value)

    if type(value) in types:
      register_type = self.get_register_by_name(name)['Type']
      if type(value).__name__ == register_type:
        return (address, value.value)
      else:
        raise TypeError("Value's type is invalid")
    else:
      raise TypeError("Value's type is invalid")
  
  def translate_from(self, address:int, value:int) -> tuple[str, Any]:
    register = self.get_register_by_address(address)
    register_type = self.name_type_mapping[register['Type']]
    typed_value = register_type(value)
    return typed_value

  def _is_value_in_range(self, value, min, max):
    if min <= value and value <= max:
      return True
    else:
      return False 
  #-------------------------------------------------------------------------------
def csv_to_list_dict(filename):
  """
  Reads a CSV file and converts it into a list of dictionaries.
  
  Args:
      filename (str): The path to the CSV file.
      
  Returns:
      list: A list where each element is a dictionary representing a row.
  """
  # Open the file in read mode
  with open(filename, mode='r', encoding='utf-8') as csvfile:
    # Create a DictReader object
    dict_reader = csv.DictReader(csvfile)
    # Convert the DictReader iterator to a list of dictionaries
    list_of_dicts = list(dict_reader)
  return list_of_dicts

registers_filename = '/Users/nickwong-openshelf/Documents/embedded_dev/perpetual_os/perpetual_hardware/packages/hardware/src/hardware/data/iclrs_registers.csv'
registers_dict = csv_to_list_dict(registers_filename)
types = [ControlMode, MotorDirection, 
  DigitalInputFunction, DigitalInputOutputMode, DigitalInputFilter, 
  DigitalInputSettings, DigitalOutputFunction, DigitalOutputSettings, IsEnabled,
  BaudRate, ControlWordFunction, HomingDirection, HomingType, HomingMode, 
  PathOperationMode, PathSettings, PathCoordinate, Alarm, MotionStatus,
  ControlWordStatus]

iclrs_registers = DeviceRegisters(registers=registers_dict, types=types)

# print(iclrs_registers.translate_to("Pulse/revolution", 500))
# print(iclrs_registers.translate_to("Control mode", ControlMode.CLOSED_LOOP))
# print(iclrs_registers.translate_to("Motor direction", MotorDirection.CCW))
# settings = {'function': DigitalInputFunction.ENABLE,
#             'mode': DigitalInputOutputMode.NORMALLY_OPENED,
#             'filter': DigitalInputFilter.FILTER_50_MS}
# print(iclrs_registers.translate_to("SI1", settings))
# settings = {'function': DigitalOutputFunction.PATH_COMPLETE,
#             'mode': DigitalInputOutputMode.NORMALLY_OPENED}
# print(iclrs_registers.translate_to("SO1", settings))
# print(iclrs_registers.translate_to("Self test", {'enable': False}))
# print(iclrs_registers.translate_to("Auto-tuning at power on", {'enable': False}))
# print(iclrs_registers.translate_to("RS485 baud rate", {'baud_rate': '115200'}))
# print(iclrs_registers.translate_to("Control word", ControlWordFunction.RESET_CURRENT_ALARM))
# settings = {'direction': HomingDirection.CCW,
#             'mode': HomingType.DETECTED_BY_LIMIT_SWITCH,
#             'move_to_specified_point_after_homing': False}
# print(iclrs_registers.translate_to("Homing mode", settings))
# settings = {'mode': PathOperationMode.POSITION_MODE,
#             'enable_interrupt': False,
#             'enable_overlapping': False,
#             'coordinate': PathCoordinate.ABSOLUTE,
#             'jump_to': 0,
#             'enable_jump': False
# }
# print(iclrs_registers.translate_to("Path 0 settings", settings))
# print(iclrs_registers.translate_to("Control mode", "ControlMode.CLOSED_LOOP"))
# try:
#   print(iclrs_registers.translate_to("Control mode", MotorDirection.CCW))
# except TypeError as e:
#   print(e)
# try:
#   print(iclrs_registers.translate_to("Pulse/revolution", 0))
# except ValueError as e:
#   print(e)

# settings = {'function': DigitalOutputFunction.PATH_COMPLETE,
#             'mode': DigitalInputOutputMode.NORMALLY_OPENED,
#             'filter': 0b1100}
# try:
#   print(iclrs_registers.translate_to("SI1", settings))
# except ValueError as e:
#   print(e)


import json
config_json_filename = '/Users/nickwong-openshelf/Documents/embedded_dev/perpetual_os/perpetual_hardware/packages/hardware/src/hardware/config/config_hardware.json'
with open(config_json_filename, 'r') as file:
  data = json.load(file)
initial_registers = data['motors'][0]['write_initial_registers']
for register_name, value in initial_registers.items():
  print(iclrs_registers.translate_to(register_name, value))

# print(initial_registers['SO2'])
# print(type(DigitalInputFunction))


controller_msgs = [(0x2203, 0x01), (0x2203, 0x02), (0x2203, 0x40), 
             (0x2203, 0x80), (0x2203, 0x200), (0x2203, 0x100),
             (0x1003, 0b000000), (0x1003, 0b100101), (0x1003, 0b111111),
             (0x1901, 0x5555), (0x1901, 0xAAAA)]
for msg in controller_msgs:
  readable = iclrs_registers.translate_from(msg[0], msg[1])
  print(f'(address, value):{msg} translates to:')
  print(readable)

