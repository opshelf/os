import csv
import inspect
from enum import Enum
from typing import Any, Union, Dict
from pydantic import (BaseModel, ValidationError, Field, field_validator,
  model_validator)

class Register(BaseModel):
    # This field will accept a standard int, or a hex string, and store it as an int
    address: int
    name: str
    min: int = Field(..., ge=0, le=0xffff)
    max: int = Field(..., ge=0, le=0xffff)
    unit: str
    description: str
    type: str = 'int'

    @field_validator('address', mode='before')
    @classmethod
    def validate_int_str(cls, v: Union[int, str]) -> int | None:
      if isinstance(v, str):
        v = v.lower()
        if v.startswith('0x'):
          # formatted as a hex string
          try:
            return int(v, 16)
          except:
            return None
        if v.startswith('0b'):
          # formatted as a binary string
          try:
            return int(v, 2)
          except:
            return None
      if isinstance(v, int):
        return v

    @model_validator(mode='after')
    def check_max_greater_than_min(self) -> 'Register':
      if self.min > self.max:
        raise ValueError("Register's min must be less than its max")
      return self

def csv_to_registers(filename:str) -> Union[list[Register], None]:
  with open(filename, mode='r', encoding='utf-8') as csvfile:
    dict_reader = csv.DictReader(csvfile)
    list_of_dicts = list(dict_reader)
    # Remove unnecessary keys
    keep_keys = [field_name for field_name in list(Register.model_fields.keys())]
    list_of_dicts = [
      {k:v for k,v in dict.items() if k in keep_keys}
      for dict in list_of_dicts
    ]
    if list_of_dicts != None:
      try:
        register_descriptions = [Register(**dict) for dict in list_of_dicts]
        return register_descriptions
      except ValidationError as e:
        print(f'Error importing csv: {e}')

def get_classes_in_module(module):
  """
  Returns a list of all classes defined within a given module.
  """
  classes = []
  # Get all members of the module and check if they are a class
  for name, member in inspect.getmembers(module, inspect.isclass):
      # Ensure the class is defined in the module's own namespace (not imported)
      if member.__module__ == module.__name__:
          classes.append(member)
  return classes

class DeviceRegisters():
  def __init__(self, registers: Union[list[Register], None], types: Union[list, None]):
    self.registers = registers
    self.types = types
    self.name_type_mapping = {type.__name__:type for type in types}
    for type in types:
      if issubclass(type, Enum):
        mapping = self._create_map_of_names_to_enum_members(type)
        self.name_type_mapping = self.name_type_mapping | mapping

  def _create_map_of_names_to_enum_members(self, enum):
    names = [member.name for member in enum]
    enum_name = enum.__name__
    mapping = {f'{enum_name}.{name}':enum[name]
             for name in names}
    return mapping

  def get_register_by_name(self, name: str) -> Register|None:
    register = next((d for d in self.registers if d.name.strip().lower() == name.strip().lower()), None)
    return register

  def get_register_by_address(self, address: int) -> Register|None:
    register = next((d for d in self.registers if d.address == address), None)
    return register

  def translate_to_address_value(self, name: str, value: Any) -> tuple[int, int]|tuple[None,None]:
    register = self.get_register_by_name(name)
    BOOL_MAP = {
        'true': True,
        'false': False,
        'yes': True,
        'no': False,
        '1': True,
        '0': False,
    }
    if register == None:
      return (None, None)
    address = register.address

    if register.type == 'int':
      min = register.min
      max = register.max
      if not self._is_value_in_range(value, min, max):
        raise ValueError("Value not in the register's valid range")
      return (address, value)

    if register.type == 'bool':

      if isinstance(value, int):
        bool_value = bool(value)
        value = int(bool_value)
      if isinstance(value, bool):
        value = int(value)
      if isinstance(value, str):
        try:
          value = int(BOOL_MAP[value])
        except KeyError:
          raise ValueError("Invalid value for bool-type")
      return (address, value)

    # if register.type == 'BaudRate':
    #   value = BaudRate(**value).value
    #   return (address, value)

    if type(value) == dict:
      print('dict')
      register_type = register.type
      MyClass = self.name_type_mapping[register_type]
      for k,v in value.items():
        if isinstance(v, str):
          try:
            value[k] = BOOL_MAP[v.lower()]
            continue
          except KeyError as e:
            pass
          try:
            value[k] = self.name_type_mapping[v]
          except:
            value[k] = v
      instance = MyClass(**value)
      value = instance.value
      return (address, value)

    if isinstance(value, str):
      if value in self.name_type_mapping:
        value = self.name_type_mapping[value]
        if type(value).__name__ == register.type:
          return (address, value.value)
        else:
          raise TypeError("Value's type is invalid")
      else:
        raise TypeError("Value's type is invalid")


    if type(value) in self.types:
      if type(value).__name__ == register.type:
        return (address, value.value)
      else:
        raise TypeError("Value's type is invalid")
    else:
      raise TypeError("Value's type is invalid")

  def translate_from(self, address:int, value:int) -> tuple[str, Any]:
    register = self.get_register_by_address(address)
    register_type = self.name_type_mapping[register.type]
    typed_value = register_type(value)
    return typed_value

  def to_json_dict(self, address:int, value:int) -> Dict:
    register = self.get_register_by_address(address)
    response_dict = {
      'address': register.address,
      'address': register.address,
      'value': value,
      'name': register.name,
      'unit': register.unit,
    }

    if register.type == 'int':
      response_dict['readable'] = f'{register.name} = {value} {register.unit}'
      return response_dict

    if register.type == 'bool':
      response_dict['readable'] = f'{register.name} = {bool(value)}'
      return response_dict

    type = self.name_type_mapping[register.type]
    if type in self.types:
      if issubclass(type, Enum):
        typed_value = type(value)
        response_dict['readable'] = f'{register.name} = {typed_value.name.lower().replace('_',' ')}'
        return response_dict
      else:
        typed_value = type(value = value)
        response_dict['readable'] = f'{register.name} = {typed_value}'
        return response_dict

  def _is_value_in_range(self, value, min, max):
    if min <= value and value <= max:
      return True
    else:
      return False
