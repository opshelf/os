import csv
import inspect
from enum import Enum, EnumMeta, IntEnum
from typing import Any, Dict, Type, Union

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass


class Register(BaseModel):
    # This field will accept a standard int, or a hex string, and store it as an int
    address: int
    name: str
    min: int = Field(..., ge=0, le=0xFFFF)
    max: int = Field(..., ge=0, le=0xFFFF)
    unit: str
    description: str
    type: Type = int

    @field_validator("address", mode="before")
    @classmethod
    def validate_int_str(cls, v: Union[int, str]) -> int:
        if isinstance(v, str):
            v = v.lower()
            try:
                if v.startswith("0x"):
                    number = int(v, 16)
                elif v.startswith("0b"):
                    number = int(v, 2)
                else:
                    raise TypeError(
                        "String address must start with '0x' or '0b'"
                    )
            except ValueError as e:
                raise TypeError(
                    "Invalid string representation of address integer"
                ) from e
            return number
        else:
            return v

    @model_validator(mode="after")
    def check_max_greater_than_min(self) -> "Register":
        if self.min > self.max:
            raise ValueError(
                "Register's min must be less than its max"
            )
        return self


class DeviceRegisters:
    def __init__(self, registers: list[Register]):
        self.registers = registers
        self.str_to_enum_mapping = {}
        reg_types = set([register.type for register in registers])
        # Find IntEnums used directly as register types
        enums = set(
            [type for type in reg_types if issubclass(type, IntEnum)]
        )
        # Find IntEnums used as fields of registers types
        for t in reg_types:
            if t != int and t != bool and not issubclass(t, IntEnum):
                a = DeviceRegisters._find_enums_in_class_annotations(
                    t
                )
                enums.update(a)
            for enum in enums:
                mapping = DeviceRegisters._create_map_of_names_to_enum_members(
                    enum
                )
                self.str_to_enum_mapping = (
                    self.str_to_enum_mapping | mapping
                )

    @staticmethod
    def csv_to_registers(
        filename: str, types: list[Type]
    ) -> list[Register]:
        try:
            with open(
                filename, mode="r", encoding="utf-8"
            ) as csvfile:
                dict_reader = csv.DictReader(csvfile)
                list_of_dicts = list(dict_reader)
        except FileNotFoundError:
            print(f"Error: The file {filename} was not found.")
            raise
        except csv.Error as e:
            print(f"Error: CSV processing error: {e}")
            raise
        except IOError:
            print(
                "Error: An I/O error occured while opening the file."
            )
            raise
        except Exception as e:
            print(f"An unexcepted error occured: {e}")
            raise
        else:
            # Defensively lowercase key names
            list_of_dicts = [
                {k.lower(): v for k, v in dict.items()}
                for dict in list_of_dicts
            ]
            # Remove unnecessary keys
            keep_keys = [
                field_name
                for field_name in list(Register.model_fields.keys())
            ]
            list_of_dicts = [
                {k: v for k, v in dict.items() if k in keep_keys}
                for dict in list_of_dicts
            ]
            # Replace type strings with actual types
            str_to_type_mapping = {
                type.__name__: type for type in types
            }
            str_to_type_mapping = str_to_type_mapping | {
                "int": int,
                "bool": bool,
                "": int,
            }
            for dict in list_of_dicts:
                dict["type"] = str_to_type_mapping[dict["type"]]
            try:
                registers = [Register(**d) for d in list_of_dicts]
            except ValidationError as e:
                print(f"Error: Invalid entry in registers csv: {e}")
                raise
            return registers

    @staticmethod
    def get_classes_in_module(module):
        """
        Returns a list of all classes defined within a given module.
        """
        classes = []
        # Get all members of the module and check if they are a class
        for name, member in inspect.getmembers(
            module, inspect.isclass
        ):
            # Ensure the class is defined in the module's own namespace (not imported)
            if member.__module__ == module.__name__:
                classes.append(member)
        return classes

    def get_register_by_name(self, name: str) -> Register:
        try:
            register = next(
                (
                    d
                    for d in self.registers
                    if d.name.strip().lower() == name.strip().lower()
                )
            )
        except StopIteration:
            raise ValueError(f"Invalid register name {name}")
        return register

    def get_register_by_address(self, address: int) -> Register:
        try:
            register = next(
                (d for d in self.registers if d.address == address),
                None,
            )
        except StopIteration:
            ValueError(f"Invalid register address {address}")
        return register

    def str_to_enum(self, value: str) -> Any:
        try:
            enum_value = self.str_to_enum_mapping[value]
        except KeyError:
            raise ValueError(f"Invalid enum string {value}")
        return enum_value

    def apply_type(self, name: str, value: int) -> Any:
        register = self.get_register_by_name(name)
        type = register.type
        if type == int:
            return value
        elif type == bool:
            return bool(value)
        elif issubclass(type, Enum):
            return type(value)
        else:
            return type(value=value)

    def get_message(self, name: str, value: int) -> str:
        register = self.get_register_by_name(name)
        type = register.type
        message = ""
        if type == int:
            message = f"{register.name} = {value} {register.unit}"
        elif type == bool:
            message = f"{register.name} = {bool(value)}"
        elif issubclass(type, Enum):
            typed_value = type(value)
            message = f"{register.name} = {typed_value.name.lower().replace('_', ' ')}"
        else:
            typed_value = type(value=value)
            message = f"{register.name} = {typed_value}"
        return message

    def get_register_model(self, name: str):
        register = self.get_register_by_name(name)
        type = register.type
        if type == int:
            min = register.min
            max = register.max
            model = create_model(
                "model", value=(int, Field(ge=min, le=max))
            )
            return model
        if type == bool:
            model = create_model("model", value=bool)
            return model
        if issubclass(type, Enum):
            model = create_model("model", value=type)
            return model
        else:
            pydantic_type = pydantic_dataclass(type)
            return pydantic_type

    @staticmethod
    def _find_enums_in_class_annotations(cls):
        enums_used = set()
        for name, type_hint in cls.__annotations__.items():
            # Check if the type hint is an Enum class or a subclass
            if isinstance(type_hint, EnumMeta):
                enums_used.add(type_hint)
        return list(enums_used)

    @staticmethod
    def _create_map_of_names_to_enum_members(enum):
        names = [member.name for member in enum]
        enum_name = enum.__name__
        mapping = {
            f"{enum_name}.{name}": enum[name] for name in names
        }
        return mapping

    @staticmethod
    def _enum_to_string(value: str) -> str:
        enum_value = (
            repr(value)
            .replace("<", "")
            .replace(">", "")
            .split(":")[0]
        )
        return enum_value

    def encode(self, name: str, value: Any) -> tuple[int, int]:
        register = self.get_register_by_name(name)
        address = register.address
        # Pre-process values, replacing strings with acceptable enum values
        if isinstance(value, str):
            value = self.str_to_enum_mapping[value]
        if isinstance(value, dict):
            value = {
                k: self.str_to_enum(v)
                for k, v in value.items()
                if isinstance(v, str)
            }
        # Create model for validation
        model = self.get_register_model(name)
        # Apply validation
        if isinstance(value, dict):
            validated_value = model(**value).value
        elif isinstance(value, list):
            validated_value = model(value=value).value.value
        else:
            validated_value = model(value=value).value
        return (address, validated_value)

    def decode(self, address: int, value: int) -> Dict[str, Any]:
        register = self.get_register_by_address(address)
        type = register.type
        if register.type == int:
            decode_dict = {register.name: value}
            return decode_dict
        elif register.type == bool:
            decode_dict = {register.name: bool(value)}
            return decode_dict
        elif issubclass(type, Enum):
            enum_str = DeviceRegisters._enum_to_string(type(value))
            decode_dict = {register.name: enum_str}
            return decode_dict
        else:
            fields = vars(type(value=value))
            del fields["value"]
            for name, value in fields.items():
                if isinstance(value, Enum):
                    fields[name] = DeviceRegisters._enum_to_string(
                        value
                    )
            decode_dict = {register.name: fields}
        return decode_dict
