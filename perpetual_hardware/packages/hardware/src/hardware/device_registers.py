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

class DeviceRegisters():
  def __init__(self, registers: list[Register], types: list[Type]):
    self.registers = registers
    self.types = types
    self.types.extend([int, bool])
    self.str_to_type_mapping = {type.__name__:type for type in types}
    self.str_to_enum_mapping = {}
    for type in types:
      if issubclass(type, Enum):
        mapping = DeviceRegisters._create_map_of_names_to_enum_members(type)
        self.str_to_enum_mapping = self.str_to_enum_mapping | mapping

  @staticmethod
  def csv_to_registers(filename:str) -> list[Register]:
    try:
      with open(filename, mode='r', encoding='utf-8') as csvfile:
        dict_reader = csv.DictReader(csvfile)
        list_of_dicts = list(dict_reader)
    except FileNotFoundError as e:
      print(f'Error: The file {filename} was not found.')
      raise
    except IOError as e:
      print(f'Error: An I/O error occured while opening the file.')
      raise
    except csv.Error as e:
      print(f'Error: CSV processing error: {e}')
      raise
    except Exception as e:
      print(f'An unexcepted error occured: {e}')
      raise
    # Defensively lowercase key names
    list_of_dicts = [
      {k.lower():v for k,v in dict.items()} for dict in list_of_dicts
    ]
    # Remove unnecessary keys
    keep_keys = [field_name for field_name in list(Register.model_fields.keys())]
    list_of_dicts = [
      {k:v for k,v in dict.items() if k in keep_keys} for dict in list_of_dicts
    ]
    try:
      register_descriptions = [Register(**d) for d in list_of_dicts]
    except ValidationError as e:
      print(f'Error importing csv: {e}')
      raise
    return register_descriptions

  @staticmethod
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
      value = self.str_to_enum_mapping[value]
    if type(value) == dict:
      value_copy = copy.deepcopy(value)
      for k, v in value_copy.items():
        try:
          value_copy[k] = self.str_to_enum_mapping[v]
        except:
          pass
      value = value_copy
    # Create validator
    _type = self.str_to_type_mapping[register.type]
    validator = DeviceRegisters._create_pydantic_model(register, _type)
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
      decode_dict = {register.name: value}
      return decode_dict
    if register.type == 'bool':
      decode_dict = {register.name: bool(value)}
      return decode_dict
    type = self.str_to_type_mapping[register.type]
    if type in self.types:
      if issubclass(type, Enum):
        enum_str = self._enum_to_string(type(value))
        decode_dict = {register.name: enum_str}
        return decode_dict
      else:
        fields = vars(type(value = value))
        del fields['value']
        for name, value in fields.items():
          if isinstance(value, Enum):
            fields[name] = self._enum_to_string(value)
        decode_dict = {register.name: fields}
        return decode_dict

  def readable(self, address:int, value:int) -> str:
    register = self.get_register_by_address(address)
    readable = ''
    if register.type == 'int':
      readable = f'{register.name} = {value} {register.unit}'
    if register.type == 'bool':
      readable = f'{register.name} = {bool(value)}'
    type = self.str_to_type_mapping[register.type]
    if type in self.types:
      if issubclass(type, Enum):
        typed_value = type(value)
        readable = f'{register.name} = {typed_value.name.lower().replace('_',' ')}'
      else:
        typed_value = type(value = value)
        readable = f'{register.name} = {typed_value}'
    return readable

  @staticmethod
  def _create_pydantic_model(register:Register, type:Type):
    type_str = register.type
    if type_str == 'int':
      min = register.min
      max = register.max
      model = create_model('model',
        value = (int, Field(ge = min, le = max)),
      )
      return model
    if type_str == 'bool':
      model = create_model('model', value = bool)
      return model
    if issubclass(type, Enum):
      model = create_model('model', value = type)
      return model
    else:
      pydantic_type = pydantic_dataclass(type)
      return pydantic_type

  @staticmethod
  def _create_map_of_names_to_enum_members(enum):
    names = [member.name for member in enum]
    enum_name = enum.__name__
    mapping = {f'{enum_name}.{name}':enum[name] for name in names}
    return mapping

  def _enum_to_string(self, value:str) -> str:
    enum_value = repr(value).replace('<','').replace('>','').split(':')[0]
    return enum_value

