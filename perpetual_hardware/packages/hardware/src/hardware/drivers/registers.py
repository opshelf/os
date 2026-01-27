import csv
import inspect
from enum import Enum, EnumMeta, IntEnum
from typing import Any, ClassVar, Dict, Type, Union

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    create_model,
    field_validator,
    model_validator,
)
from pydantic.dataclasses import dataclass as pydantic_dataclass

from hardware.controller import SharedModbusController
from hardware.drivers.utilty_functions import (
    coerce_str_to_int,
    create_map_of_names_to_enum_members,
    find_enums_in_class_type_hints,
)
from hardware.exceptions import (
    MotorError,
)


class Register(BaseModel):
    # This field will accept a standard int, or a hex string, and store it as an int
    address: int
    name: str
    min: int = Field(0, ge=0, le=0xFFFF)
    max: int = Field(0xFFFF, ge=0, le=0xFFFF)
    unit: str
    description: str
    type: Type = int

    @field_validator('*')
    @classmethod
    def clean_up_str(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
        return v

    @field_validator("address", mode="before")
    @classmethod
    def validate_address(cls, v: Union[int, str]) -> int:
        if isinstance(v, str):
            return coerce_str_to_int(v)
        elif isinstance(v, int):
            return v
        else:
            raise TypeError("address must be int or str")

    @field_validator("min", "max", mode="before")
    @classmethod
    def validate_min_max(
        cls, v: Union[int, str], info=ValidationInfo
    ) -> int:
        if isinstance(v, str):
            v = v.strip()
            if v == "":
                field_name = info.field_name
                field_info = info.model_fields.get(field_name)
                default = field_info.default
                return default
            else:
                return coerce_str_to_int(v)
        elif isinstance(v, int):
            return v
        else:
            raise TypeError("min and max must be int or str")

    @field_validator("type", mode="before")
    @classmethod
    def validate_type(cls, v: Union[str,Type], info:ValidationInfo) -> Type:
        if isinstance(v, str):
            if info.context is None:
                raise ValueError("no context provided for string-type mapping")
            str_to_type_mapping:Dict[str, Type] = info.context.get("str_to_type_mapping", {})
            try:
                type = str_to_type_mapping[v]
            except KeyError:
                raise ValueError(f"Invalid type string {v}")
            return type
        elif inspect.isclass(v):
            return v
        else:
            raise TypeError("type must be str or Type")

    @model_validator(mode="after")
    def check_max_greater_than_min(self) -> "Register":
        if self.min > self.max:
            raise ValueError(
                "register's min must be less than its max"
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
            if (
                t is not int
                and t is not bool
                and not issubclass(t, IntEnum)
            ):
                a = find_enums_in_class_type_hints(t)
                enums.update(a)
            for enum in enums:
                mapping = create_map_of_names_to_enum_members(enum)
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
                # Process headers:
                if dict_reader.fieldnames is None:
                    raise ValueError("CSV file has no header row")
                dict_reader.fieldnames = [
                    field.strip().lower()
                    for field in dict_reader.fieldnames
                ]
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
            # Remove unnecessary keys
            keep_keys = [
                field_name
                for field_name in list(Register.model_fields.keys())
            ]
            list_of_dicts = [
                {k: v for k, v in dict.items() if k in keep_keys}
                for dict in list_of_dicts
            ]
            str_to_type_mapping = {
                type.__name__: type for type in types
            }
            str_to_type_mapping = str_to_type_mapping | {
                "int": int,
                "bool": bool,
                "": int, # default to int if no type specified
            }
            context_data = {"str_to_type_mapping": str_to_type_mapping}
            registers = [Register.model_validate(d, context=context_data) for d in list_of_dicts]
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
                (d for d in self.registers if d.address == address))
        except StopIteration:
            raise ValueError(f"Invalid register address {address}")
        else:
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
        if type is int:
            return value
        elif type is bool:
            return bool(value)
        elif issubclass(type, Enum):
            return type(value)
        else:
            return type(value=value)

    def get_message(self, name: str, value: int) -> str:
        register = self.get_register_by_name(name)
        type = register.type
        message = ""
        if type is int:
            message = f"{register.name} = {value} {register.unit}"
        elif type is bool:
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
        if type is int:
            min = register.min
            max = register.max
            model = create_model(
                "model", value=(int, Field(ge=min, le=max))
            )
            return model
        if type is bool:
            model = create_model("model", value=bool)
            return model
        if issubclass(type, Enum):
            model = create_model("model", value=type)
            return model
        else:
            pydantic_type = pydantic_dataclass(type)
            return pydantic_type

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
        if register.type is int:
            decode_dict = {register.name: value}
            return decode_dict
        elif register.type is bool:
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


# --- Generic Register BaseModel ---
class DeviceRegistersModel(BaseModel):
    # Either 'name' or 'address' must be provided as input.
    name: str  # 'name' is the register name.
    address: int | None = None  # 'address' is the register address.
    value: Any | None = (
        None  # 'value' is the input representation of the value.
    )
    # Post-validation fields:
    modbus: tuple[int, int] | None = (
        None  # 'modbus' is (address, value).
    )
    typed: Any | None = (
        None  # 'typed' is the value with type applied.
    )
    message: str | None = (
        None  # 'message' is any human-readable message.
    )
    # set registers to a specific DeviceRegister
    REGISTERS:ClassVar[DeviceRegisters]

    @model_validator(mode="before")
    @classmethod
    def name_address_check(cls, values: Any) -> Any:
        if isinstance(values, dict) and "address" in values:
            address = values["address"]
            try:
                register = cls.REGISTERS.get_register_by_address(
                    address
                )
            except ValueError:
                raise ValueError("Invalid register address")
            if "name" in values and values["name"] != register.name:
                raise ValueError(
                    "Provided name does not match address"
                )
            values["name"] = register.name
        return values

    @field_validator("name", mode="before")
    def valid_register_name(cls, v: str) -> str:
        v = v.lower()
        try:
            cls.REGISTERS.get_register_by_name(v)
        except ValueError as e:
            raise ValueError("Invalid register name") from e
        return v

    @field_validator("value", mode="before")
    def value_must_be_of_a_valid_type(
        cls, v: Any | None
    ) -> Any | None:
        # Must be int, bool, dict, or an acceptable IntEnum.
        # If a string, coerce it into an acceptable IntEnum.
        if v is None:
            return v
        if isinstance(v, str):
            try:
                v = cls.REGISTERS.str_to_enum(v)
            except ValueError as e:
                raise ValueError(
                    "Invalid string for register value"
                ) from e
        # If a dict, coerce any strings in the dict into acceptable IntEnums.
        if isinstance(v, dict):
            try:

                def f(_v):
                    if isinstance(_v, str):
                        return cls.REGISTERS.str_to_enum(_v)
                    else:
                        return _v

                v = {_k: f(_v) for _k, _v in v.items()}
            except ValueError as e:
                raise ValueError(
                    "Invalid string in dict for register value"
                ) from e
        return v

    @model_validator(mode="after")
    def valid_value_for_name(self) -> "DeviceRegisterModel":
        name = self.name
        address = (
            type(self).REGISTERS.get_register_by_name(name).address
        )
        self.address = address
        value = self.value
        if value is not None:
            model = type(self).REGISTERS.get_register_model(name)
            try:
                if isinstance(value, dict):
                    v = model(**value)
                    valid_value = v.value
                elif isinstance(value, IntEnum):
                    valid_value = model(value=value).value.value
                else:
                    valid_value = model(value=value).value
                    if isinstance(valid_value, bool):
                        valid_value = int(valid_value)
            except ValidationError as e:
                raise ValueError("Invalid value for register") from e
            # Post-validation processing:
            self.modbus = (address, valid_value)
            self.typed = type(self).REGISTERS.apply_type(
                name, valid_value
            )
            self.message = type(self).REGISTERS.get_message(
                name, valid_value
            )
        return self

def registers_model_factory(registers:list[Register]):
    device_registers = DeviceRegisters(registers)
    class DeviceRegisterModel(DeviceRegistersModel):
        REGISTERS = device_registers
    return DeviceRegisterModel

class DeviceRegistersCommunication:
    def __init__(
        self,
        com: SharedModbusController,
        id: int,
        register_model: type[DeviceRegistersModel],
    ):
        self._com = com
        self._id = id
        self._register_model = register_model

    async def read_register(self, name: str) -> DeviceRegistersModel:
        """Get a motor parameter register value.

        Args:
            register: Name of the register to read

        Raises:
            MotorError: If parameter read fails
            ValueError: If register name is invalid

        Returns:
            Register value as integer, or None if read fails
        """
        # Get address from name
        try:
            register = self._register_model(name=name)
        except ValidationError as e:
            raise ValueError(f"Invalid register name: {name}") from e
        address = register.address
        # Read address via Modbus
        try:
            value = await self._com.read_holding_registers(
                self._id, address
            )
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(
                    f"Failed to get parameter {register}: {str(e)}"
                ) from e
            raise

        if value is None:
            raise MotorError(f"Failed to read register {register}")
        else:
            return self._register_model(name=name, value=value)

    async def write_register(self, name: str, value: Any) -> None:
        """Set a motor parameter register.

                Args:
            register: Name of the register to set
            value: Value to write to the register

        Raises:
            MotorError: If parameter write fails
            ValueError: If register name is invalid
        """

        # Get address from name
        try:
            register = self._register_model(name=name, value=value)
        except ValidationError as e:
            raise ValueError(f"Invalid register: {e}") from e
        # Write register via modbus
        try:
            await self._com.write_single_register(
                self._id, register.modbus[0], register.modbus[1]
            )
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(
                    f"Failed to set parameter {register}: {str(e)}"
                ) from e
            raise
