import asyncio
import logging
import time

from enum import Enum
from hardware.controller import SharedModbusController
from hardware.exceptions import MotorError, MotorTimeoutError

logger = logging.getLogger(__name__)


class ServoStatus(Enum):
    IDLE = 0
    MOVING = 1
    STOPPED = 2
    ERROR = 3
    FAULT = 4
    TIMEOUT = 5


class Servo:
    def __init__(self, controller: SharedModbusController, params: dict | None = None):
        if params is None:
            params = {}

        self._motor_id = int(params.get("motor_id", 0x9))
        self._controller = controller
        self._control_reg = int(params.get("control_reg", 0x3E8))
        self._status_reg = int(params.get("status_reg", 0x07D0))
        self._configured = False

    async def configure(self) -> None:
        try:
            await self._controller.write_single_register(
                self._motor_id, 
                self._control_reg, 
                0x0
            )

            await self._controller.write_single_register(
                self._motor_id, 
                self._control_reg, 
                0x1
            )

            await self.wait_for_activate()

            await self._controller.write_single_register(
                self._motor_id, 
                0x03EB, 
                0x8020
            )

            await self._controller.write_single_register(
                self._motor_id, 
                0x03ED, 
                0x80FE
            )

            await self._controller.write_single_register(
                self._motor_id, 
                0x03EF, 
                0x80C0
            )

            await self._controller.write_single_register(self._motor_id, 0x03EC, 0x80)
            await self._controller.write_single_register(self._motor_id, 0x03EE, 0x80)
            await self._controller.write_single_register(self._motor_id, 0x03F0, 0x80)
            self._configured = True
        except Exception as e:
            raise MotorError(f"Failed to configure servo: {str(e)}") from e

    async def wait_for_activate(self):
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(self._motor_id, self._status_reg)

            if status is None:
                raise MotorError("Failed to read motor status during activation")

            activation_status = status & (1<<5)

            if activation_status:
                return

            if time.monotonic() - start_time > 6:
                raise MotorTimeoutError("Motor activation timed out after 6 seconds")

            await asyncio.sleep(0.01)

    async def wait_for_servo(self, timeout: float = 5.0) -> ServoStatus:
        start_time = time.monotonic()

        while True:
            status = await self._controller.read_holding_registers(self._motor_id, self._status_reg)

            if status is None:
                raise MotorError("Failed to read motor status during servo motion")
                
            move_status = (status & (1<<6)) or (status & (1<<7))

            if move_status:
                return ServoStatus.STOPPED
                
            if time.monotonic() - start_time > timeout:
                raise MotorTimeoutError(f"Servo motion timed out after {timeout} seconds")
                
            await asyncio.sleep(0.01)


    async def _ensure_configured(self) -> None:
        if self._configured is False:
            await self.configure()

    async def open(self):
        await self._ensure_configured()
        try:
            await self._controller.write_single_register(self._motor_id, self._control_reg, 0x080B)
            await self.wait_for_servo()
        except Exception as e:
            raise MotorError(f"Failed to open servo: {str(e)}") from e

    async def safe_open(self):
        await self._ensure_configured()
        try:
            await self._controller.write_single_register(self._motor_id, self._control_reg, 0x0A0B)
            await self.wait_for_servo()
        except Exception as e:
            raise MotorError(f"Failed to safe-open servo: {str(e)}") from e

    async def close(self):
        await self._ensure_configured()
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x090B) 
        await self.wait_for_servo()
