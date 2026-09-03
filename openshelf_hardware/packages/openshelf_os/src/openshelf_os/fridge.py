from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Literal, Optional

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor
from openshelf_os.config.settings import DemoFridgeCellSettings, DemoFridgeSettings
from openshelf_os.exceptions import ConfigError, FridgeSafetyError, MotorError
from openshelf_os.storage import StorageModuleCellLocation


class DemoFridge:
    """Demo-only sliding fridge door and per-cell access configuration."""

    def __init__(
        self,
        motorcontroller: Controller,
        params: DemoFridgeSettings,
        is_fully_retracted: Callable[[], Awaitable[bool]],
        safety=None,
        *,
        is_rotated_back: Callable[[], Awaitable[bool]] | None = None,
        retract_tool: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if params.motor is None or params.motor.enabled is False:
            raise ConfigError("Demo fridge motor must be configured and enabled")

        self._params = params
        self._door = Motor(motorcontroller, params.motor, safety)
        self._is_fully_retracted = is_fully_retracted
        self._is_rotated_back = is_rotated_back
        self._retract_tool = retract_tool
        self._logger = logging.getLogger(__name__).getChild("DemoFridge")
        self._motion_lock = asyncio.Lock()
        self._init_reporter: Optional[
            Callable[[str, str, str, Optional[str]], None]
        ] = None
        self._cells = {
            self._location_key_from_config(cell): cell
            for cell in params.cells
        }

    def update_settings(self, params: DemoFridgeSettings) -> None:
        """Apply saved door calibration without reconstructing the hardware object."""
        if params.motor is None or params.motor.enabled is False:
            raise ConfigError("Demo fridge motor must be configured and enabled")

        self._params = params
        self._door._params = params.motor
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
            raise FridgeSafetyError(
                "Cannot verify tool retraction before moving fridge door"
            ) from exc
        if is_retracted is not True:
            raise FridgeSafetyError(
                "Tool must be fully retracted before moving fridge door"
            )

    async def _prepare_tool_for_door_motion(self) -> None:
        # Preserve the original guard for lightweight callers that do not have
        # access to rotation state. Robot runtime always supplies both callbacks.
        if self._is_rotated_back is None:
            await self._require_tool_retracted()
            return

        try:
            is_rotated_back = await self._is_rotated_back()
        except Exception as exc:
            raise FridgeSafetyError(
                "Cannot verify tool rotation before moving fridge door"
            ) from exc

        if is_rotated_back is not True:
            return

        try:
            is_retracted = await self._is_fully_retracted()
        except Exception as exc:
            raise FridgeSafetyError(
                "Cannot verify tool retraction before moving fridge door"
            ) from exc

        if is_retracted is True:
            return
        if self._retract_tool is None:
            raise FridgeSafetyError(
                "Tool must be fully retracted before moving fridge door"
            )

        try:
            await self._retract_tool()
        except Exception as exc:
            raise FridgeSafetyError(
                "Failed to retract tool before moving fridge door"
            ) from exc

        await self._require_tool_retracted()

    async def home_door(self) -> None:
        try:
            await self._prepare_tool_for_door_motion()
            async with self._motion_lock:
                await self._door.home()
                await self._move_door_to_locked(self.position_for_name("center"))
            self._emit_init("ready")
        except FridgeSafetyError as exc:
            self._emit_init("failed", detail="home", reason=str(exc))
            raise
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
        await self.move_door_to_named(direction)

    def position_for_name(self, position: Literal["left", "center", "right"]) -> float:
        positions = self._params.door_positions_mm
        if positions is not None:
            return {
                "left": float(positions.open_left),
                "center": float(positions.closed),
                "right": float(positions.open_right),
            }[position]

        if position != "center":
            raise ConfigError("Fridge door positions have not been calibrated")

        motor = self._params.motor
        if motor is None:
            raise ConfigError("Fridge door motor has not been configured")
        return (float(motor.lower_lim) + float(motor.lim)) / 2.0

    async def move_door_to_named(
        self,
        position: Literal["left", "center", "right"],
    ) -> None:
        await self.move_door_to(self.position_for_name(position))

    async def _move_door_to_locked(self, target: float) -> None:
        motor = self._params.motor
        if motor is None:
            raise ConfigError("Fridge door motor has not been configured")
        await self._door.move_to_position(target, rpm=motor.rpm)

    async def move_door_to(self, target: float) -> None:
        await self._prepare_tool_for_door_motion()

        try:
            async with self._motion_lock:
                await self._move_door_to_locked(target)
        except Exception as exc:
            raise MotorError(f"Failed to move fridge door to {target}mm: {exc}") from exc

    async def close_door(self) -> None:
        try:
            await self.move_door_to_named("center")
        except FridgeSafetyError:
            raise
        except Exception as exc:
            raise MotorError(f"Failed to close fridge door: {exc}") from exc
