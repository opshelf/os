import csv
import inspect
from enum import Enum
from typing import Any, Union, Dict, Type
from pydantic import (BaseModel, ValidationError, Field, field_validator,
  model_validator, create_model)
from pydantic.dataclasses import dataclass as pydantic_dataclass
import copy

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

def create_pydantic_model(register:Register, type:Type):
  type_str = register.type
  if type_str == 'int':
    model = create_model('model',
      value = (int, Field(ge=register.min, le=register.max)),
    )
    return model
  if type_str == 'bool':
    model = create_model('model',
      value = bool,
    )
    return model
  if issubclass(type, Enum):
    model = create_model('model',
      value = type,
    )
    return model
  else:
    pydantic_type = pydantic_dataclass(type)
    return pydantic_type

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
        register_descriptions = [Register(address = d['address'],
                                          name = d['name'],
                                          min = d['min'],
                                          max = d['max'],
                                          unit = d['unit'],
                                          description = d['description'],
                                          type = d['type'])
                                for d in list_of_dicts]
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
    self.types.extend([int, bool])
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

  def encode(self, name: str, value: Any) -> tuple[int, int]|tuple[None,None]:
    register = self.get_register_by_name(name)
    if register == None:
      return (None, None)
    address = register.address
    # Pre-process values
    if isinstance(value, str):
      value = self.name_type_mapping[value]
    if type(value) == dict:
      value_copy = copy.deepcopy(value)
      for k,v in value_copy.items():
        try:
          value_copy[k] = self.name_type_mapping[v]
        except:
          pass
      value = value_copy
    # Create validator
    _type = self.name_type_mapping[register.type]
    validator = create_pydantic_model(register, _type)
    # Apply validation
    if type(value) in self.types and type(value) not in [int, bool]:
      if type(value).__name__ == register.type:
        validated_value = value.value
    if type(value) == dict:
      validated_value = validator(**value).value
    else:
      validated_value = validator(value = value).value

    return (address, validated_value)

  def decode(self, address:int, value:int) -> Dict[str, Any]:
    register = self.get_register_by_address(address)
    if register.type == 'int':
      decode_dict = {
        register.name: value,
      }
      return decode_dict
    if register.type == 'bool':
      decode_dict = {
        register.name: bool(value),
      }
      return decode_dict
    type = self.name_type_mapping[register.type]
    if type in self.types:
      if issubclass(type, Enum):
        enum_str = self._enum_to_string(type(value))
        decode_dict = {
          register.name: enum_str,
        }
        return decode_dict
      else:
        fields = vars(type(value = value))
        del fields['value']
        for name, value in fields.items():
          if isinstance(value, Enum):
            fields[name] = self._enum_to_string(value)
        decode_dict = {
          register.name: fields,
        }
        return decode_dict

  def readable(self, address:int, value:int) -> str:
    register = self.get_register_by_address(address)
    if register.type == 'int':
      readable = f'{register.name} = {value} {register.unit}'
      return readable
    if register.type == 'bool':
      readable = f'{register.name} = {bool(value)}'
      return readable
    type = self.name_type_mapping[register.type]
    if type in self.types:
      if issubclass(type, Enum):
        typed_value = type(value)
        readable = f'{register.name} = {typed_value.name.lower().replace('_',' ')}'
        return readable
      else:
        typed_value = type(value = value)
        readable = f'{register.name} = {typed_value}'
        return readable

  def _enum_to_string(self, value):
    enum_value = repr(value).replace('<','').replace('>','').split(':')[0]
    return enum_value

  def _is_value_in_range(self, value, min, max):
    if min <= value and value <= max:
      return True
    else:
      return False