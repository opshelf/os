import asyncio
import logging
import time
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
    field_validator,
    model_validator,
)

from hardware.controller import SharedModbusController
from hardware.drivers import iclrs_register_types
from hardware.drivers.iclrs_register_types import (
    Alarm,
    AlarmCode,
    ControlWordFunction,
    ControlWordStatus,
    MotionStatus,
    PathControlCommand,
    PathOperationMode,
)
from hardware.drivers.registers import (
    DeviceRegisters,
    DriverClient,
    RegistersModel,
    registers_model_factory,
)
from hardware.drivers.utilty_functions import (
    create_clamped_value_model,
)
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

# --- Create register model ---
REGISTERS_FILENAME = "../src/hardware/device_docs/icl-rs/ICL42.csv"
TYPES = DeviceRegisters.get_classes_in_module(iclrs_register_types)
registers = DeviceRegisters.csv_to_registers(
    REGISTERS_FILENAME, TYPES
)
IclrsRegister: type[RegistersModel] = registers_model_factory(
    registers
)


# --- Icl-Rs Config BaseModel ---
class IclrsConfig(BaseModel):
    motor_name: str = Field(..., min_length=1)
    motor_id: int = Field(..., ge=0)  # tighten if desired
    enabled: bool = Field(default=True)
    lim: Union[int, float] = Field(..., gt=0)
    mpr: float = Field(..., gt=0)
    phys_at_zero: Optional[Union[int, float]] = Field(default=None)
    phys_at_max: Optional[Union[int, float]] = Field(default=None)
    configuration_registers: List[IclrsRegister]

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


# --- Icl-Rs Driver ---
class Iclrs:
    # _motor_id: int
    # _controller: SharedModbusController
    # _params: Any
    # _registers: DeviceRegisters
    _stall_timeout: float
    _status: MotorStatus
    _cur_pos: Optional[int]

    def __init__(
        self,
        controller: SharedModbusController,
        params: IclrsConfig,
        safety: Optional[CurtainSafety] = None,
    ) -> None:
        self._controller: SharedModbusController = controller
        self._config: IclrsConfig = params
        self._motor_id: int = params.motor_id
        self.client: DriverClient = DriverClient(
            self._controller, self._motor_id, IclrsRegister
        )
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
        self._position_validator = create_clamped_value_model(
            "clamped_position", 0, params.lim
        )
        self._stop_event = asyncio.Event()

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
            await self.client.write_register(
                name="Control word",
                value=ControlWordFunction.RESET_ALL_PARAMETERS,
            )
            # start = time.monotonic()
            # for r in self._config.configuration_registers:
            #     await self.client.write_register(r.name, r.value)
            # end = time.monotonic()
            # print(f"duration: {end - start}")

            start = time.monotonic()
            list_regs = [
                (r.name, r.value)
                for r in self._config.configuration_registers
            ]
            await self.client.write_registers(list_regs)
            end = time.monotonic()
            print(f"duration: {end - start}")

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
        # Homing settings (stall time, speed, etc) are set in the config file.

        # Clear alarms before homing, so that we can detect any new alarms, in
        # particular the alarm for stall torque.
        await self.clear_alarms()
        await self.trigger_homing()
        await self.wait_for_idle(timeout_s=20)
        # Status checks:
        alarm = await self.client.read_register("alarm")
        if alarm.typed.code == AlarmCode.STALL_TORQUE:
            # Stall torque achieved, indicating home position.
            await self.client.write_register(
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
        await self.client.write_register(
            "path 0 settings",
            value={"mode": PathOperationMode.POSITION_MODE},
        )
        await self.client.write_register(
            "path 0 position (high bits)", value=position_hb
        )
        await self.client.write_register(
            "path 0 position (low bits)", value=position_lb
        )
        await self.client.write_register("path 0 velocity", value=rpm)
        await self.client.write_register(
            "path 0 acceleration", value=accel
        )
        await self.client.write_register(
            "path 0 deceleration", value=accel
        )
        # Trigger path
        await self.trigger_path()
        # Wait to finish
        await self.wait_for_idle(timeout_s=30)
        # Motor is now done running, but need to check if it stopped because of fault
        # or because it actually completed the path.
        status = await self.client.read_register("motion status")
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
        await self.client.write_register(
            "path 0 settings",
            value={"mode": PathOperationMode.POSITION_MODE},
        )
        await self.client.write_register(
            "path 0 position (high bits)", value=position_hb
        )
        await self.client.write_register(
            "path 0 position (low bits)", value=position_lb
        )
        await self.client.write_register("path 0 velocity", value=rpm)
        await self.client.write_register(
            "path 0 acceleration", value=accel
        )
        await self.client.write_register(
            "path 0 deceleration", value=accel
        )
        # Trigger path
        await self.trigger_path()
        # Clear stop_event
        self._stop_event.clear()

    async def wait_for_idle(self, timeout_s: int = 10) -> None:
        start_time_s = time.monotonic()
        while True:
            await asyncio.sleep(0.2)
            # Poll status
            status = await self.client.read_register("motion status")
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
        alarm = await self.client.read_register("alarm")
        return alarm

    async def clear_alarms(self) -> None:
        # Clear any existing alarms.
        await self.client.write_register(
            name="Control word",
            value=ControlWordFunction.RESET_CURRENT_ALARM,
        )
        await self.client.write_register(
            name="Control word",
            value=ControlWordFunction.RESET_HISTORY_ALARM,
        )

    async def trigger_path(self, path: int = 0) -> None:
        await self.client.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.PATH_MOTION,
                "path": path,
            },
        )

    async def trigger_homing(self) -> None:
        await self.client.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.HOMING,
            },
        )

    async def zero_position(self) -> None:
        await self.client.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.ZERO_CURRENT_POSITION,
            },
        )

    async def read_position(self) -> int:
        position_hb = await self.client.read_register(
            "position (high bits)"
        )
        position_lb = await self.client.read_register(
            "position (low bits)"
        )
        position = position_hb.value << 16 | position_lb.value
        return position

    async def read_status(self) -> MotionStatus:
        status = await self.client.read_register("motion status")
        return status.typed

    async def stop(self) -> None:
        self._stop_event.set()
        await self.client.write_register(
            name="pr control",
            value={"command": PathControlCommand.QUICK_STOP},
        )

    async def set_soft_limit(self, limit_position: int) -> None:
        bytes = limit_position.to_bytes(2)
        await self.client.write_register(
            name="soft limit+ (high bits)", value=bytes[0]
        )
        await self.client.write_register(
            name="soft limit- (high bits)", value=bytes[1]
        )

    async def write_control_word_and_read_status(
        self, function: ControlWordFunction
    ) -> IclrsRegister:
        await self.client.write_register(
            "control word", value=function
        )
        status_register = await self.client.read_register(
            "control word status"
        )
        return status_register

    @staticmethod
    def calculate_mpr_to_ppr(
        mpr: int, mm_per_pulse: float = 0.001
    ) -> int:
        # mpr - mm per revolution
        # mm_per_pulse - mm per pulse

        ppr = int(1 / mm_per_pulse * mpr)
        print(ppr)
        return ppr

    async def reset_registers(self):
        status_register = (
            await self.write_control_word_and_read_status(
                ControlWordFunction.RESET_ALL_PARAMETERS
            )
        )
        status_code = status_register.typed
        if status_code == ControlWordStatus.OK:
            pass
        elif status_code == ControlWordStatus.FAULT:
            pass
        await asyncio.sleep(0.5)
