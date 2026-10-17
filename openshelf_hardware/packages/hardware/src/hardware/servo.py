import asyncio
import logging
import time

from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Optional
from hardware.controller import SharedModbusController
from hardware.exceptions import MotorError, MotorTimeoutError

logger = logging.getLogger(__name__)


# Fault bit 2 is comm-loss (>1 s idle bus); the manual says it does not affect motion.
FAULT_COMM_LOSS = 1 << 2
# A new command can report the previous move's gOBJ until the drive picks it up.
STALE_OBJ_WINDOW_S = 0.15


class GripObject(IntEnum):
    """gOBJ (status bits 6-7)."""
    MOVING = 0
    INNER = 1
    OUTER = 2
    NONE = 3


@dataclass(frozen=True)
class GripState:
    """Decoded 0x07D0-0x07D2: status, fault, position (gPR), current (gCU)."""
    status: int
    fault: int
    position: int
    current: int

    @property
    def obj(self) -> GripObject:
        return GripObject((self.status >> 6) & 0x3)

    @property
    def dropped(self) -> bool:
        return bool(self.status & 0x2)

    @property
    def gripped(self) -> bool:
        return self.obj in (GripObject.INNER, GripObject.OUTER) and not self.dropped

    def __str__(self) -> str:
        return (f"obj={self.obj.name} drop={int(self.dropped)} pos=0x{self.position:02X} "
                f"cu=0x{self.current:02X} fault=0x{self.fault:02X}")


class Servo:
    def __init__(self, controller: SharedModbusController, params: dict | None = None):
        if params is None:
            params = {}

        self._motor_id = int(params.get("motor_id", 0x9))
        self._controller = controller
        self._control_reg = int(params.get("control_reg", 0x3E8))
        self._status_reg = int(params.get("status_reg", 0x07D0))
        self._open_position = int(params.get("open_position", 0x20)) & 0xFF
        self._configured = False
        self._on_init_progress: Optional[Callable[[str, str, Optional[str]], None]] = None

    def _emit_init(self, phase: str, detail: str = "", reason: Optional[str] = None) -> None:
        reporter = getattr(self, "_on_init_progress", None)
        if reporter is None:
            return
        try:
            reporter(phase, detail, reason)
        except Exception:
            pass

    async def configure(self) -> None:
        try:
            self._emit_init("configuring")
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
                0x8000 | self._open_position
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
            self._emit_init("ready")
        except Exception as e:
            self._emit_init("failed", detail="configure", reason=str(e))
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

    async def read_state(self) -> GripState:
        """Read status, fault, position and current in one transaction."""
        r = await self._controller.read_holding_register_block(self._motor_id, self._status_reg, 3)
        return GripState(r[0] & 0xFF, r[1] & 0xFF, (r[1] >> 8) & 0xFF, (r[2] >> 8) & 0xFF)

    async def wait_for_servo(self, timeout: float = 5.0) -> GripState:
        """Poll until gOBJ reports a result; raise on drive faults."""
        start_time = time.monotonic()
        seen_moving = False

        while True:
            state = await self.read_state()

            if state.fault & ~FAULT_COMM_LOSS:
                raise MotorError(f"Gripper fault: {state}")

            if state.obj is GripObject.MOVING:
                seen_moving = True
            elif seen_moving or time.monotonic() - start_time > STALE_OBJ_WINDOW_S:
                return state

            if time.monotonic() - start_time > timeout:
                raise MotorTimeoutError(f"Servo motion timed out after {timeout} seconds")

            await asyncio.sleep(0.01)

    async def _ensure_configured(self) -> None:
        if self._configured is False:
            await self.configure()

    async def open(self) -> GripState:
        """Preset 1: splay fingers (inner-support grip)."""
        await self._ensure_configured()
        try:
            await self._controller.write_single_register(self._motor_id, self._control_reg, 0x080B)
            return await self.wait_for_servo()
        except Exception as e:
            raise MotorError(f"Failed to open servo: {str(e)}") from e

    async def safe_open(self) -> GripState:
        await self._ensure_configured()
        try:
            await self._controller.write_single_register(self._motor_id, self._control_reg, 0x0A0B)
            return await self.wait_for_servo()
        except Exception as e:
            raise MotorError(f"Failed to safe-open servo: {str(e)}") from e

    async def close(self) -> GripState:
        """Preset 2: retract fingers (release)."""
        await self._ensure_configured()
        await self._controller.write_single_register(self._motor_id, self._control_reg, 0x090B)
        return await self.wait_for_servo()
