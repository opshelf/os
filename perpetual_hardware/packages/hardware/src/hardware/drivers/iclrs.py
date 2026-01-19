import logging
from enum import IntEnum
from typing import Any, Awaitable, Callable, List, Optional, Union

from pydantic import (
    BaseModel,
    Field,
    RootModel,
    ValidationError,
    field_validator,
    model_validator,
)

from hardware.controller import SharedModbusController
from hardware.drivers import iclrs_register_types
from hardware.drivers.registers import DeviceRegisters
from hardware.exceptions import (
    MotorConfigError,
)
from hardware.motor import CurtainSafety, MotorStatus

logger = logging.getLogger(__name__)

# TODO: Move to config
STEPPER_STALL_TIMEOUT_S = 2.0
STEPPER_BRAKE_RELEASE_DELAY_MS = 100
STEPPER_BRAKE_LOCKED_DELAY_MS = 100
STEPPER_THRESHOLD_VALUE_OF_BRAKE_LOCKING_VELOCITY = 10
STEPPER_POSITION_LOOP_KP = 50
STEPPER_MAX_STOP_TIME = 100
MAX_FOLLOWING_ERROR = 4000

# Icl-Rs DeviceRegister.
REGISTERS_FILENAME = "../src/hardware/device_docs/icl-rs/ICL42.csv"
TYPES = DeviceRegisters.get_classes_in_module(iclrs_register_types)
registers = DeviceRegisters.csv_to_registers(REGISTERS_FILENAME, TYPES)
iclrs_registers = DeviceRegisters(registers)

# Icl-Rs Register BaseModel
class IclrsRegister(BaseModel):
    # Either 'name' or 'address' must be provided as input.
    name: str|None = None       # 'name' is the register name.
    address: int|None = None    # 'address' is the register address.
    value: Any                  # 'value' is the input representation of the value.
    # Post-validation fields:
    modbus: tuple[int,int]|None = None  # 'modbus' is (address, value).
    typed: Any|None = None              # 'typed' is the value with type applied.
    message: str|None = None            # 'message' is any human-readable message.

    @model_validator(mode="before")
    @classmethod
    def name_address_check(cls, values: Any) -> Any:
        if isinstance(values, dict) and 'address' in values:
            address = values['address']
            try:
                register = iclrs_registers.get_register_by_address(address)
            except ValueError:
                raise ValueError("Invalid register address")
            if 'name' in values and values['name'] != register.name:
                raise ValueError("Provided name does not match address")
            values['name'] = register.name
        return values

    @field_validator("name", mode="before")
    def name_must_in_registers_list(cls, v: str) -> str:
        v = v.lower()
        if v not in iclrs_registers.names:
            raise ValueError("Invalid register name")
        return v

    @field_validator("value", mode="before")
    def value_must_be_of_a_valid_type(cls, v: Any) -> Any:
        # Must be int, bool, dict, or an acceptable IntEnum.
        # If a string, coerce it into an acceptable IntEnum.
        if isinstance(v, str):
            try:
                v = iclrs_registers.str_to_enum(v)
            except ValueError as e:
                raise ValueError("Invalid string for register value") from e
        # If a dict, coerce any strings in the dict into acceptable IntEnums.
        if isinstance(v, dict):
            try:
                v = {_k: iclrs_registers.str_to_enum(_v) for _k, _v in v.items() if isinstance(_v, str)}
            except ValueError as e:
                raise ValueError("Invalid string in dict for register value") from e
        return v

    @model_validator(mode='after')
    def valid_value_for_name(self) -> 'IclrsRegister':
        name = self.name
        value = self.value
        model = iclrs_registers.get_register_model(name)
        if self.address is None:
            self.address = iclrs_registers.get_register_by_name(name).address
        try:
            if isinstance(self.value, dict):
                valid_value = model(**value).value
            elif isinstance(self.value, IntEnum):
                valid_value = model(value = value).value.value
            else:
                valid_value = model(value = value).value
        except ValidationError as e:
            raise ValueError('Invalid value for register') from e
        # Post-validation processing:
        address = iclrs_registers.get_register_by_name(name).address
        self.modbus = (address, valid_value)
        self.typed = iclrs_registers.apply_type(name, valid_value)
        self.message = iclrs_registers.get_message(name, valid_value)
        return self

class IclrsRegisters(RootModel):
    # The 'root' attribute holds the list of Iclrs models
    root: List[IclrsRegister]

# Icl-Rs Config BaseModel
class IclrsConfig(BaseModel):
    motor_name: str = Field(..., min_length=1)
    motor_id: int = Field(..., ge=0)  # tighten if desired
    enabled: bool = Field(default=True)
    lim: Union[int, float] = Field(..., gt=0)
    mpr: float = Field(..., gt=0)
    phys_at_zero: Optional[Union[int, float]] = Field(default=None)
    phys_at_max: Optional[Union[int, float]] = Field(default=None)
    configuration_registers: IclrsRegisters

    @field_validator("motor_name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("motor_name cannot be empty/whitespace")
        return s

    # @field_validator("lim")
    # @classmethod
    # def _lim_positive(cls, v):
    #     return _positive(v, "lim")

    @model_validator(mode="after")
    def _validate_physical_endpoints(self) -> "IclrsConfig":
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim

        if p0 == pm:
            raise ValueError(
                f"Motor '{self.motor_name}': phys_at_zero ({p0}) and "
                f"phys_at_max ({pm}) cannot be equal"
            )

        phys_min = min(p0, pm)
        phys_max = max(p0, pm)

        if phys_min < 0:
            raise ValueError(
                f"Motor '{self.motor_name}': physical endpoints must be >= 0, "
                f"got phys_at_zero={p0}, phys_at_max={pm}"
            )

        if phys_max > self.lim:
            raise ValueError(
                f"Motor '{self.motor_name}': physical endpoints must be <= lim ({self.lim}), "
                f"got phys_at_zero={p0}, phys_at_max={pm}"
            )

        return self

    def logical_to_physical(self, logical: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim
        return p0 + (logical / self.lim) * (pm - p0)

    def physical_to_logical(self, physical: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim
        return (physical - p0) / (pm - p0) * self.lim

    def logical_to_physical_delta(self, logical_delta: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = self.phys_at_max if self.phys_at_max is not None else self.lim
        scale = (pm - p0) / self.lim
        return logical_delta * scale

class Iclrs:
    _motor_id: int
    _controller: SharedModbusController
    _params: Any
    _stall_timeout: float
    _status: MotorStatus
    _cur_pos: Optional[int]
    _registers: DeviceRegisters

    def __init__(
        self,
        controller: SharedModbusController,
        params: IclrsConfig,
        safety: Optional[CurtainSafety] = None,
    ) -> None:
        self._controller = controller
        self._config = params
        self._motor_id = params.motor_id
        self._cur_pos = None
        self._stall_timeout = STEPPER_STALL_TIMEOUT_S
        self._status = MotorStatus.IDLE
        self._pending_motion: Optional[Callable[[], Awaitable[None]]] = None
        self._target_pos: Optional[float] = None
        self._logger = logger.getChild(f"motor.{self._motor_id}")
        self._safety = safety
        self._simulated_fault = False

    async def configure(self):
        """Configure the stepper motor with initial parameters.
        NOTE: Configuration register values should be explicitly defined in the
        config json file, not here.

        Raises:
            MotorConfigError: If any configuration parameter fails to set
            MotorError: If communication with motor fails
        """
        try:
            self._logger.info(
                "Configuring motor",
                extra={"motor_id": self._motor_id, "limit_mm": self._config.lim},
            )
            # await self.clear_alarms()
            for r in self._config.configuration_registers.root:
                print(r)
        except Exception as e:
            self._logger.error(
                "Failed to configure motor",
                extra={"motor_id": self._motor_id, "error": str(e)},
                exc_info=True,
            )
            raise MotorConfigError(
                f"Failed to configure motor {self._motor_id}: {str(e)}"
            ) from e

    # async def clear_alarms(self):
    #   # Clear any existing alarms.
    #   await self.set_param("Control word", ControlWordFunction.RESET_CURRENT_ALARM)
    #   await self.set_param("Control word", ControlWordFunction.RESET_HISTORY_ALARM)

    # async def move_config(self) -> None:
    #   """Configure motor movement parameters.

    #   Raises:
    #       MotorConfigError: If configuration fails
    #   """
    #   try:
    #     await self.clear_alarms()
    #     # Disable auto-homing on power-on to prevent blocking motion commands.
    #     value = self._registers.encode("pr global control", {})[1] # 0x0000
    #     await self.set_param("pr global control", value)
    #     # Configure movement parameters
    #     await self.set_param("Motor direction", self._params.dr)
    #     await self.set_param("Velocity", self._params.rpm)
    #     await self.set_param("Motion of Path 0", 0x1)  # abs pos 0x1
    #   except Exception as e:
    #     raise MotorConfigError(f"Failed to configure movement parameters: {str(e)}") from e

    # # TODO add guards on setters and getters
    # async def set_param(self, register_name: str, value: int) -> None:
    #   """Set a motor parameter register.

    #   Args:
    #       register: Name of the register to set
    #       value: Value to write to the register

    #   Raises:
    #       MotorError: If parameter write fails
    #       ValueError: If register name is invalid
    #   """
    #   try:
    #     register = self._registers.get_register_by_name(register_name)
    #     if register is None:
    #       raise ValueError(f"Invalid register name: {register}")
    #     await self._controller.write_single_register(self._motor_id, register.address, value)
    #   # if result is None:
    #   #     raise MotorError(f"Failed to write {value} to register {register}")
    #   except Exception as e:
    #     if not isinstance(e, (MotorError, ValueError)):
    #       raise MotorError(f"Failed to set parameter {register_name}: {str(e)}") from e

    # # TODO add guards on setters and getters
    # async def write_register(self, name: str, value: Any) -> None:
    #   """Set a motor parameter register.

    #   Args:
    #       register: Name of the register to set
    #       value: Value to write to the register. May be an int, bool, dict, or an
    #       IntEnum that can be encoded into a valid value for the register.

    #   Raises:
    #       MotorError: If parameter write fails
    #       ValueError: If register name is invalid
    #   """
    #   try:
    #     register = self._registers.get_register_by_name(name)
    #     if register is None:
    #       raise ValueError(f"Invalid register name: {name}")
    #     address, encoded_value = self._registers.encode(name, {
    #       "function": "DigitalOutputFunction.PATH_COMPLETE",
    #       "mode": "DigitalInputOutputMode.NORMALLY_OPENED"}
    #     )
    #     if address == None:
    #       raise ValueError(f"Invalid register name: {name}")
    #     elif encoded_value == None:
    #       raise ValueError(f"Invalid register value: {value}")
    #     else:
    #       await self._controller.write_single_register(self._motor_id, address, encoded_value)
    #   except Exception as e:
    #     if not isinstance(e, (MotorError, ValueError)):
    #       raise MotorError(f"Failed to write register {name}: {str(e)}") from e