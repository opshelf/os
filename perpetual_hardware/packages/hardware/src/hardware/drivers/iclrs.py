import asyncio
import logging
import struct
import time
from typing import (
    Awaitable,
    Callable,
    List,
    Optional,
)

from pydantic import (
    BaseModel,
    Field,
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
    get_data_path,
)
from hardware.exceptions import (
    MotorConfigError,
    MotorError,
    MotorTimeoutError,
)
from hardware.motor import CurtainSafety, MotorStatus

logger = logging.getLogger(__name__)

# --- Create register model ---
REGISTERS_FILENAME = get_data_path("ICL42.csv")
TYPES = DeviceRegisters.get_classes_in_module(iclrs_register_types)
registers = DeviceRegisters.csv_to_registers(
    REGISTERS_FILENAME, TYPES
)
IclrsRegister: type[RegistersModel] = registers_model_factory(
    registers
)


# --- Icl-Rs Config BaseModel ---
class IclrsConfig(BaseModel):
    modbus_id: int = Field(..., ge=1, le=31)
    configuration_registers: List[IclrsRegister]


# --- Icl-Rs Driver ---
class Iclrs:
    def __init__(
        self,
        controller: SharedModbusController,
        config: IclrsConfig,
    ) -> None:
        self._controller: SharedModbusController = controller
        self._config: IclrsConfig = config

        self.motor_id: int = config.modbus_id
        self.client: DriverClient = DriverClient(
            self._controller, self.motor_id, IclrsRegister
        )

        self._pending_motion: Optional[
            Callable[[], Awaitable[None]]
        ] = None
        self._target_pos: Optional[float] = None
        self._logger = logger.getChild(f"motor.{self.motor_id}")
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
                extra={
                    "motor_id": self.motor_id,
                    # "limit_mm": self._config.lim,
                },
            )
            await self.client.write_register(
                name="Control word",
                value=ControlWordFunction.RESET_ALL_PARAMETERS,
            )

            list_regs = [
                (r.name, r.value)
                for r in self._config.configuration_registers
            ]
            await self.client.write_registers(list_regs)

        except Exception as e:
            self._logger.error(
                "Failed to configure motor",
                extra={"motor_id": self.motor_id, "error": str(e)},
                exc_info=True,
            )
            raise MotorConfigError(
                f"Failed to configure motor {self.motor_id}: {str(e)}"
            ) from e

    async def read_pulse_per_revolution(self) -> int:
        response = await self.client.read_register(
            "pulse per revolution"
        )
        return response.value

    async def write_homing_command(self) -> None:
        # We home by the method of "stall zeroing," which uses stall condition as the
        # home switch.
        # Homing settings (stall time, speed, etc) are set in the config file.
        # Clear alarms before homing, so that we can detect any new alarms, in
        # particular the alarm for stall torque.
        await self.clear_alarms()
        await self.client.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.HOMING,
            },
        )

    async def write_move_to_position(
        self, position: int, rpm: int, accel: int
    ) -> None:
        # Position is given as an signed int, convert it to representative unsigned int.
        # This ensures that we don't signed negative values to the register.
        # signed=True: use two's complement for negative numbers.
        position_bytes = position.to_bytes(
            4, byteorder="big", signed=True
        )
        position_hb = int.from_bytes(
            position_bytes[0:2], byteorder="big", signed=False
        )
        position_lb = int.from_bytes(
            position_bytes[2:4], byteorder="big", signed=False
        )

        # position_hb = position >> 16
        # position_lb = position & 0xFFFF
        move_command_registers = [
            (
                "path 0 settings",
                {"mode": PathOperationMode.POSITION_MODE},
            ),
            ("path 0 position (high bits)", position_hb),
            ("path 0 position (low bits)", position_lb),
            ("path 0 velocity", rpm),
            ("path 0 acceleration", accel),
            ("path 0 deceleration", accel),
            ("path 0 pause time", 0),
        ]
        await self.client.write_registers(move_command_registers)
        await self.client.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.PATH_MOTION,
                "path": 0,
            },
        )

    async def read_alarm(self) -> Alarm:
        alarm = await self.client.read_register("alarm")
        return alarm.typed

    async def clear_current_alarm(self) -> None:
        await self.client.write_register(
            name="Control word",
            value=ControlWordFunction.RESET_CURRENT_ALARM,
        )

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
        # Confirm that alarms were cleared.
        alarm = await self.read_alarm()
        if alarm.code != AlarmCode.NO_ALARM:
            raise MotorError(
                f"Alarm {alarm_code} persists after clear attempt"
            )

    # async def trigger_homing(self) -> None:
    #     await self.client.write_register(
    #         name="pr control",
    #         value={
    #             "command": PathControlCommand.HOMING,
    #         },
    #     )

    async def zero_position(self) -> None:
        await self.client.write_register(
            name="pr control",
            value={
                "command": PathControlCommand.ZERO_CURRENT_POSITION,
            },
        )

    async def read_position(self) -> int:
        position_hb = await self.client.read_register(
            "actual position (high bits)"
        )
        position_lb = await self.client.read_register(
            "actual position (low bits)"
        )
        # Convert to signed 32-bit integer
        position_signed = Iclrs.position_bytes_to_value(
            position_hb.value, position_lb.value
        )
        return position_signed

    async def read_status(self) -> MotionStatus:
        status = await self.client.read_register("motion status")
        return status.typed

    async def write_stop_command(self) -> None:
        await self.client.write_register(
            name="pr control",
            value={"command": PathControlCommand.QUICK_STOP},
        )

    async def write_control_word_and_read_status(
        self, function: ControlWordFunction
    ) -> RegistersModel:
        await self.client.write_register(
            "control word", value=function
        )
        status_register = await self.client.read_register(
            "control word status"
        )
        return status_register

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

    @staticmethod
    def position_bytes_to_value(
        position_hb: int, position_lb: int
    ) -> int:
        # Convert to signed 32-bit integer
        position_unsigned = position_hb << 16 | position_lb
        packed_bytes = struct.pack(">I", position_unsigned)
        position_signed = struct.unpack(">i", packed_bytes)[0]
        return position_signed
