from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Literal, Optional

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor
from openshelf_os.config.settings import DemoFridgeCellSettings, DemoFridgeSettings
from openshelf_os.exceptions import ConfigError, MotorError
from openshelf_os.storage import StorageModuleCellLocation


class DemoFridge:
    """Demo-only sliding fridge door and per-cell access configuration."""

    def __init__(
        self,
        motorcontroller: Controller,
        params: DemoFridgeSettings,
        is_fully_retracted: Callable[[], Awaitable[bool]],
        safety=None,
    ) -> None:
        if params.motor is None or params.motor.enabled is False:
            raise ConfigError("Demo fridge motor must be configured and enabled")

        self._params = params
        self._door = Motor(motorcontroller, params.motor, safety)
        self._is_fully_retracted = is_fully_retracted
        self._logger = logging.getLogger(__name__).getChild("DemoFridge")
        self._motion_lock = asyncio.Lock()
        self._init_reporter: Optional[
            Callable[[str, str, str, Optional[str]], None]
        ] = None
        self._cells = {
            self._location_key_from_config(cell): cell
            for cell in params.cells
        }

    @staticmethod
    def _location_key_from_config(cell: DemoFridgeCellSettings) -> tuple[str, int, int]:
        location = cell.location
        return location.side, location.shelf_idx, location.cell_idx

    @staticmethod
    def _location_key(location: StorageModuleCellLocation) -> tuple[str, int, int]:
        return location.side.value, location.shelf_idx, location.cell_idx

    def cell_settings(
        self,
        location: StorageModuleCellLocation,
    ) -> DemoFridgeCellSettings | None:
        return self._cells.get(self._location_key(location))

    def is_fridge_cell(self, location: StorageModuleCellLocation) -> bool:
        return self.cell_settings(location) is not None

    def _emit_init(
        self,
        phase: str,
        detail: str = "",
        reason: Optional[str] = None,
    ) -> None:
        if self._init_reporter is None:
            return
        try:
            self._init_reporter("fridge_door", phase, detail, reason)
        except Exception:
            pass

    async def configure(self, *, isolate: bool = False, home_on_init: bool = True) -> None:
        self._emit_init("configuring")
        try:
            await self._door.configure()
            if home_on_init is True and self._params.enabled is True:
                await self.home_door()
            else:
                self._emit_init("ready")
        except Exception as exc:
            self._emit_init("failed", reason=str(exc))
            if isolate is True:
                self._logger.warning("init fridge door configure failed: %s", exc)
                return
            raise MotorError(f"Fridge door configuration failed: {exc}") from exc

    async def stop(self) -> None:
        try:
            await self._door.stop()
        except Exception as exc:
            raise MotorError(f"Failed to stop fridge door: {exc}") from exc

    def cleanup(self) -> None:
        self._door.cleanup()

    async def _require_tool_retracted(self) -> None:
        try:
            is_retracted = await self._is_fully_retracted()
        except Exception as exc:
            raise MotorError("Cannot verify tool retraction before moving fridge door") from exc
        if is_retracted is not True:
            raise MotorError("Tool must be fully retracted before moving fridge door")

    async def home_door(self) -> None:
        positions = self._params.door_positions_mm

        try:
            await self._require_tool_retracted()
            async with self._motion_lock:
                await self._door.home()
                if positions is not None:
                    await self._door.move_to_position(
                        positions.closed,
                        rpm=self._params.motor.ret_rpm if self._params.motor is not None else None,
                    )
            self._emit_init("ready")
        except Exception as exc:
            self._emit_init("failed", detail="home", reason=str(exc))
            raise MotorError(f"Failed to home fridge door: {exc}") from exc

    async def open_for_cell(self, location: StorageModuleCellLocation) -> None:
        cell = self.cell_settings(location)
        if cell is None:
            raise ConfigError(
                "Location is not configured as a demo fridge cell: "
                f"{location.side.value}:{location.shelf_idx}:{location.cell_idx}"
            )
        await self.open_door(cell.door_open_direction)

    async def open_door(self, direction: Literal["left", "right"]) -> None:
        positions = self._params.door_positions_mm
        if positions is None:
            raise ConfigError("Fridge door positions have not been calibrated")
        target = positions.open_left if direction == "left" else positions.open_right

        try:
            async with self._motion_lock:
                await self._door.move_to_position(target)
        except Exception as exc:
            raise MotorError(f"Failed to open fridge door {direction}: {exc}") from exc

    async def close_door(self) -> None:
        positions = self._params.door_positions_mm
        if positions is None:
            raise ConfigError("Fridge door positions have not been calibrated")

        try:
            await self._require_tool_retracted()
            async with self._motion_lock:
                await self._door.move_to_position(
                    positions.closed,
                    rpm=self._params.motor.ret_rpm if self._params.motor is not None else None,
                )
        except Exception as exc:
            raise MotorError(f"Failed to close fridge door: {exc}") from exc
