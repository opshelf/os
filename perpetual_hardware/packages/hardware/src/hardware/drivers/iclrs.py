import asyncio
import logging
import time
from enum import IntEnum
from typing import (
    Any,
    Awaitable,
    Callable,
    List,
    Optional,
    Union,
)

from pydantic import (
    BaseModel,
    Field,
    RootModel,
    ValidationError,
    create_model,
    field_validator,
    model_validator,
)

from hardware.controller import SharedModbusController
from hardware.drivers import iclrs_register_types
from hardware.drivers.iclrs_register_types import (
    Alarm,
    AlarmCode,
    ControlWordFunction,
    HomingDirection,
    HomingType,
    MotionStatus,
    PathControlCommand,
    PathOperationMode,
)
from hardware.drivers.registers import DeviceRegisters
from hardware.exceptions import (
    MotorConfigError,
    MotorError,
    MotorTimeoutError,
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

# --- Icl-Rs DeviceRegister ---
REGISTERS_FILENAME = "../src/hardware/device_docs/icl-rs/ICL42.csv"
TYPES = DeviceRegisters.get_classes_in_module(iclrs_register_types)
registers = DeviceRegisters.csv_to_registers(
    REGISTERS_FILENAME, TYPES
)
iclrs_registers = DeviceRegisters(registers)


# --- Icl-Rs Register BaseModel ---
class IclrsRegister(BaseModel):
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

    @model_validator(mode="before")
    @classmethod
    def name_address_check(cls, values: Any) -> Any:
        if isinstance(values, dict) and "address" in values:
            address = values["address"]
            try:
                register = iclrs_registers.get_register_by_address(
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
            iclrs_registers.get_register_by_name(v)
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
                v = iclrs_registers.str_to_enum(v)
            except ValueError as e:
                raise ValueError(
                    "Invalid string for register value"
                ) from e
        # If a dict, coerce any strings in the dict into acceptable IntEnums.
        if isinstance(v, dict):
            try:

                def f(_v):
                    if isinstance(_v, str):
                        return iclrs_registers.str_to_enum(_v)
                    else:
                        return _v

                v = {_k: f(_v) for _k, _v in v.items()}
            except ValueError as e:
                raise ValueError(
                    "Invalid string in dict for register value"
                ) from e
        return v

    @model_validator(mode="after")
    def valid_value_for_name(self) -> "IclrsRegister":
        name = self.name
        address = iclrs_registers.get_register_by_name(name).address
        self.address = address
        value = self.value
        if value is not None:
            model = iclrs_registers.get_register_model(name)
            try:
                if isinstance(value, dict):
                    v = model(**value)
                    valid_value = v.value
                elif isinstance(value, IntEnum):
                    valid_value = model(value=value).value.value
                else:
                    valid_value = model(value=value).value
            except ValidationError as e:
                raise ValueError("Invalid value for register") from e
            # Post-validation processing:
            self.modbus = (address, valid_value)
            self.typed = iclrs_registers.apply_type(name, valid_value)
            self.message = iclrs_registers.get_message(
                name, valid_value
            )
        return self


class IclrsRegisters(RootModel):
    # The 'root' attribute holds the list of Iclrs models
    root: List[IclrsRegister]


# --- Icl-Rs Config BaseModel ---
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

    @model_validator(mode="after")
    def _validate_physical_endpoints(self) -> "IclrsConfig":
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = (
            self.phys_at_max
            if self.phys_at_max is not None
            else self.lim
        )

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
        pm = (
            self.phys_at_max
            if self.phys_at_max is not None
            else self.lim
        )
        return p0 + (logical / self.lim) * (pm - p0)

    def physical_to_logical(self, physical: float) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = (
            self.phys_at_max
            if self.phys_at_max is not None
            else self.lim
        )
        return (physical - p0) / (pm - p0) * self.lim

    def logical_to_physical_delta(
        self, logical_delta: float
    ) -> float:
        p0 = self.phys_at_zero if self.phys_at_zero is not None else 0
        pm = (
            self.phys_at_max
            if self.phys_at_max is not None
            else self.lim
        )
        scale = (pm - p0) / self.lim
        return logical_delta * scale


# --- Validator for clamped value ---
@model_validator(mode="after")
def clamp_value(model_instance: BaseModel) -> BaseModel:
    model_field = model_instance.__class__.model_fields[
        "clamped_value"
    ]
    ge = model_field.metadata[0].ge
    le = model_field.metadata[1].le
    if model_instance.value > le:
        model_instance.clamped_value = le
    elif model_instance.value < ge:
        model_instance.clamped_value = ge
    else:
        model_instance.clamped_value = model_instance.value
    return model_instance


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
        self._pending_motion: Optional[
            Callable[[], Awaitable[None]]
        ] = None
        self._target_pos: Optional[float] = None
        self._logger = logger.getChild(f"motor.{self._motor_id}")
        self._safety = safety
        self._simulated_fault = False
        self._position_validator = (
            Iclrs._create_position_with_limits_model(0, 0xFFFFFFFF)
        )
        self._stop_event = asyncio.Event()

    async def read_register(self, name: str) -> IclrsRegister:
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
            register = IclrsRegister(name=name)
        except ValidationError as e:
            raise ValueError(f"Invalid register name: {name}") from e
        address = register.address
        # Read address via Modbus
        try:
            value = await self._controller.read_holding_registers(
                self._motor_id, address
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
            return IclrsRegister(name=name, value=value)

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
            register = IclrsRegister(name=name, value=value)
        except ValidationError as e:
            raise ValueError(f"Invalid register: {e}") from e
        # Write register via modbus
        try:
            await self._controller.write_single_register(
                self._motor_id, register.modbus[0], register.modbus[1]
            )
        except Exception as e:
            if not isinstance(e, (MotorError, ValueError)):
                raise MotorError(
                    f"Failed to set parameter {register}: {str(e)}"
                ) from e
            raise

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
                extra={
                    "motor_id": self._motor_id,
                    "limit_mm": self._config.lim,
                },
            )
            # await self.clear_alarms()
            await self.write_register(
                name="Control word",
                value=ControlWordFunction.RESET_ALL_PARAMETERS,
            )
            for r in self._config.configuration_registers.root:
                await self.write_register(r.name, r.value)
        except Exception as e:
            self._logger.error(
                "Failed to configure motor",
                extra={"motor_id": self._motor_id, "error": str(e)},
                exc_info=True,
            )
            raise MotorConfigError(
                f"Failed to configure motor {self._motor_id}: {str(e)}"
            ) from e

    async def _read_status(self) -> MotorStatus:
        pass

    async def home(self) -> None:
        # We home by the method of "stall zeroing," which uses stall condition as the
        # home switch.
        await self.write_register(
            name="homing mode",
            value={
                "direction": HomingDirection.CCW,
                "move_to_specified_point_after_homing": True,
                "mode": HomingType.DETECTED_BY_LIMIT_SWITCH,
            },
        )
        await self.write_register(
            name="torque zero return time", value=50
        )
        await self.write_register(
            name="torque zero return value", value=5
        )
        # These are the device default values, to be tuned if needed.
        await self.write_register(
            name="homing stop position (low bits)", value=0
        )
        await self.write_register(
            name="homing high velocity", value=200
        )
        await self.write_register(
            name="homing low velocity", value=50
        )
        await self.write_register(
            name="homing acceleration", value=100
        )
        await self.write_register(
            name="homing deceleration", value=100
        )
        # Clear alarms before homing, so that we can detect any new alarms, in
        # particular the alarm for stall torque.
        await self.clear_alarms()
        await self.trigger_homing()
        await self.wait_for_idle(timeout_s=20)
        # Status checks:
        alarm = await self.read_register("alarm")
        if alarm.typed.code == AlarmCode.STALL_TORQUE:
            # Stall torque achieved, indicating home position.
            await self.write_register(
                name="Control word",
                value=ControlWordFunction.RESET_CURRENT_ALARM,
            )
            await self.zero_position()
        else:
            # Stopped without stall torque, due to some other unintended fault.
            raise MotorError(
                f"Motor {self._motor_id} faulted during homing"
            )

    async def move_to_position(
        self,
        value: int,
        rpm: int,
        accel: int = 100,
    ) -> None:
        position_hb = value >> 16
        position_lb = value & 0xFFFF
        # Configure path
        await self.write_register(
            "path 0 settings",
            value={"mode": PathOperationMode.POSITION_MODE},
        )
        await self.write_register(
            "path 0 position (high bits)", value=position_hb
        )
        await self.write_register(
            "path 0 position (low bits)", value=position_lb
        )
        await self.write_register("path 0 velocity", value=rpm)
        await self.write_register("path 0 acceleration", value=accel)
        await self.write_register("path 0 deceleration", value=accel)
        # Trigger path
        await self.trigger_path()
        # Wait to finish
        await self.wait_for_idle(timeout_s=30)
        # Motor is now done running, but need to check if it stopped because of fault
        # or because it actually completed the path.
        status = await self.read_register("motion status")
        if status.typed.is_fault:
            raise MotorError(
                f"Motor {self._motor_id} faulted during homing"
            )

    async def move_to_position_no_wait(
        self,
        value: int,
        rpm: int,
        accel: int = 100,
    ) -> None:
        position_hb = value >> 16
        position_lb = value & 0xFFFF
        # Configure path
        await self.write_register(
            "path 0 settings",
            value={"mode": PathOperationMode.POSITION_MODE},
        )
        await self.write_register(
            "path 0 position (high bits)", value=position_hb
        )
        await self.write_register(
            "path 0 position (low bits)", value=position_lb
        )
        await self.write_register("path 0 velocity", value=rpm)
        await self.write_register("path 0 acceleration", value=accel)
        await self.write_register("path 0 deceleration", value=accel)
        # Trigger path
        await self.trigger_path()
        # Clear stop_event
        self._stop_event.clear()

    async def wait_for_idle(self, timeout_s: int = 10) -> None:
        start_time_s = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            # Poll status
            status = await self.read_register("motion status")
            if not status.typed.is_running:
                break  # Stop waiting
            # Timeout
            elapsed_s = start_time_s - time.monotonic()
            if elapsed_s > timeout_s:
                raise MotorTimeoutError(
                    f"Motor {self._motor_id} wait for idle timed out"
                )
            if self._stop_event.is_set():
                break

    async def read_alarm(self) -> Alarm:
        alarm = await self.read_register("alarm")
        return alarm

    async def clear_alarms(self) -> None:
        # Clear any existing alarms.
        await self.write_register(
            name="Control word",
            value=ControlWordFunction.RESET_CURRENT_ALARM,
        )
        await self.write_register(
            name="Control word",
            value=ControlWordFunction.RESET_HISTORY_ALARM,
        )

    async def trigger_path(self, path: int = 0) -> None:
        await self.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.PATH_MOTION,
                "path": path,
            },
        )

    async def trigger_homing(self) -> None:
        await self.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.HOMING,
            },
        )

    async def zero_position(self) -> None:
        await self.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.ZERO_CURRENT_POSITION,
            },
        )

    async def read_position(self) -> int:
        position_hb = await self.read_register("position (high bits)")
        position_lb = await self.read_register("position (low bits)")
        position = position_hb.value << 16 | position_lb.value
        return position

    async def read_status(self) -> MotionStatus:
        status = await self.read_register("motion status")
        return status.typed

    async def stop(self) -> None:
        self._stop_event.set()
        await self.write_register(
            name="pr control",
            value={"command": PathControlCommand.QUICK_STOP},
        )

    async def set_soft_limit(self, limit_position: int) -> None:
        bytes = limit_position.to_bytes(2)
        await self.write_register(
            name="soft limit+ (high bits)", value=bytes[0]
        )
        await self.write_register(
            name="soft limit- (high bits)", value=bytes[1]
        )

    async def write_control_word_and_read_status(
        self, function: ControlWordFunction
    ) -> IclrsRegister:
        await self.write_register("control word", value=function)
        register = await self.read_register("control word status")
        return register

    @staticmethod
    def calculate_mpr_to_ppr(
        mpr: int, mm_per_pulse: float = 0.001
    ) -> int:
        # mpr - mm per revolution
        # mm_per_pulse - mm per pulse

        ppr = int(1 / mm_per_pulse * mpr)
        print(ppr)
        return ppr

    @staticmethod
    def _create_position_with_limits_model(
        min: Union[int, float] = 0,
        max: Union[int, float] = 0xFFFFFFFF,
    ):
        model = create_model(
            f"position_with_limits_{min}_{max}",
            value=(int | float, Field(default=0)),
            clamped_value=(
                int | float | None,
                Field(ge=min, le=max, default=None),
            ),
            __validators__={"clamp_value": clamp_value},
        )
        return model

    def set_position_limit_mm(self, limit: int | float = 0xFFFFFFFF):
        self._position_validator = (
            Iclrs._create_position_with_limits_model(0, limit)
        )

    async def reset_registers(self):
        status = await self.write_control_word_and_read_status(
            ControlWordFunction.RESET_ALL_PARAMETERS
        )
        await asyncio.sleep(0.5)


# TODO
# 1. Add Position Basemodel for valid range validation - DONE
# 2. Implement soft limit
# 3. stop() - DONE
# 4. calculate_ppr() - DONE
