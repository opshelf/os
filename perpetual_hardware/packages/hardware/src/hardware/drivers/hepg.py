import asyncio
import time
from typing import ClassVar, List

from pydantic import BaseModel, Field, ValidationError

from hardware.controller import SharedModbusController
from hardware.drivers import hepg_register_types
from hardware.drivers.hepg_register_types import (
    ControlFunction,
    FingerStatus,
    GripperCommand,
    GripperMode,
    StatusCode,
)
from hardware.drivers.registers import (
    DeviceRegisters,
    DriverClient,
    RegistersModel,
    registers_model_factory,
)
from hardware.exceptions import (
    MotorConfigError,
    MotorError,
    MotorTimeoutError,
)

# --- HEPG DeviceRegister ---
REGISTERS_FILENAME = "../src/hardware/device_docs/hepg/HEPG.csv"
TYPES = DeviceRegisters.get_classes_in_module(hepg_register_types)
registers = DeviceRegisters.csv_to_registers(
    REGISTERS_FILENAME, TYPES
)

# --- HEPG DeviceRegisterModel ---
HepgRegister: type[RegistersModel] = registers_model_factory(
    registers
)


# --- HEPG Config BaseModel ---
class HepgConfig(BaseModel):
    modbus_address: int = Field(default=9, ge=0)
    enabled: bool = Field(default=True)
    activation_timeout_s: int = Field(default=3)
    configuration_registers: List[HepgRegister]


# Seperate Servo and Stepper classes into dedicated files.
# Servo used for the end effector
class Hepg:
    """Controls a servo motor via Modbus."""

    def __init__(
        self,
        controller: SharedModbusController,
        params: HepgConfig,
    ):
        self._controller = controller
        self._config = params
        self._motor_id = params.modbus_address
        self.activation_timeout_s = params.activation_timeout_s
        self.client: DriverClient = DriverClient(
            self._controller, self._motor_id, HepgRegister
        )

    async def configure(self) -> None:
        """Configure the servo with initial settings.

        Raises:
            MotorError: If configuration fails
            MotorTimeoutError: If activation times out
        """
        try:
            # Reset the gripper (clearing all fault status) and then activating.
            await self.reset()

            # start = time.monotonic()
            # # Write configuration register values.
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
            raise MotorError(
                f"Failed to configure servo: {str(e)}"
            ) from e

    async def reset(self):
        """Wait for motor to activate.

        Raises:
            MotorTimeoutError: If activation takes too long
            MotorError: If motor status read fails
        """
        # Disable the gripper, resetting it and clearing all fault status
        await self.client.write_register(
            name="control", value={"enable_gripper": False}
        )
        # Enable the gripper
        await self.client.write_register(
            name="control", value={"enable_gripper": True}
        )

        start_time = time.monotonic()
        while True:
            response = await self.client.read_register(
                name="gripper status"
            )
            activation_status = response.typed.status
            if activation_status == StatusCode.ACTIVATED:
                return
            if (
                time.monotonic() - start_time
                > self.activation_timeout_s
            ):
                raise MotorTimeoutError(
                    "Motor activation timed out after 3 seconds"
                )
            await asyncio.sleep(0.5)

    async def wait_for_servo(
        self, timeout: float = 5.0
    ) -> MotorStatus:
        """Wait for servo to complete its motion.

        Args:
            timeout: Maximum time to wait in seconds

        Raises:
            MotorTimeoutError: If servo motion takes longer than timeout
            MotorError: If motor status read fails
        """
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(
                self._motor_id, self._status_reg
            )
            if status is None:
                raise MotorError(
                    "Failed to read motor status during servo motion"
                )

            move_status = (status & (1 << 6)) or (status & (1 << 7))
            if move_status:
                return MotorStatus.STOPPED

            if time.monotonic() - start_time > timeout:
                raise MotorTimeoutError(
                    f"Servo motion timed out after {timeout} seconds"
                )

            await asyncio.sleep(0.01)

    async def move(self, command: ControlFunction):
        try:
            await self.client.write_register(
                name="control",
                value={
                    "enable_gripper": True,
                    "mode": GripperMode.PRESET_CONTROL,
                    "command": GripperCommand.MOVE_TO_TARGET_POSITION,
                    "function": command,
                },
            )
        except Exception as e:
            raise MotorError(f"Failed to open servo: {str(e)}") from e
        start_time = time.monotonic()
        while True:
            response = await self.client.read_register(
                name="gripper status"
            )
            finger_status = response.typed.finger_status
            if not finger_status == FingerStatus.MOVING_TO_POSITION:
                print(finger_status)
                return response
            if (
                time.monotonic() - start_time
                > self.activation_timeout_s
            ):
                raise MotorTimeoutError(
                    "Motor activation timed out after 3 seconds"
                )
            await asyncio.sleep(0.1)

    async def stop(self):
        await self.client.write_register(
            name="control",
            value={
                "enable_gripper": True,
                "command": GripperCommand.STOP,
            },
        )
        start_time = time.monotonic()
        while True:
            response = await self.client.read_register(
                name="gripper status"
            )
            command = response.typed.command
            if command == GripperCommand.STOP:
                return response
            if (
                time.monotonic() - start_time
                > self.activation_timeout_s
            ):
                raise MotorTimeoutError(
                    "Motor activation timed out after 3 seconds"
                )
            await asyncio.sleep(0.1)

    async def open(self):
        """Open the servo to maximum position.

        Raises:
            MotorError: If servo control fails
            MotorTimeoutError: If operation times out
        """
        try:
            self.move(command=ControlFunction.PRESET_1)
        except Exception as e:
            raise MotorError(f"Failed to open servo: {str(e)}") from e

    async def safe_open(self):
        """Open the servo to a safe intermediate position.

        Raises:
            MotorError: If servo control fails
            MotorTimeoutError: If operation times out
        """
        try:
            self.move(command=ControlFunction.PRESET_1)
        except Exception as e:
            raise MotorError(
                f"Failed to safe-open servo: {str(e)}"
            ) from e

    async def close(self):
        try:
            self.move(command=ControlFunction.PRESET_2)
        except Exception as e:
            raise MotorError(
                f"Failed to close servo: {str(e)}"
            ) from e
