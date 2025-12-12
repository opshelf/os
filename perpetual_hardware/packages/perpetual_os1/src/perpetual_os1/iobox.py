from typing import Optional

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor
from hardware.curtain import CurtainSensor, CurtainMonitor
from perpetual_os1.exceptions import (
    IOBoxError, MotorError
)

from hardware.scale import Scale
from enum import Enum
import logging
import sentry_sdk
import time


class IOBoxStatus(Enum):
    IDLE = 0
    READY = 1
    ERROR = 2

class IOBox:
    def __init__(self, 
                 motorcontroller: Controller,
                 scalecontroller: Controller, 
                 params: dict,
                 safety=None,
                 ):
        self._scalecontroller = scalecontroller
        self._motorcontroller = motorcontroller
        self._status = IOBoxStatus.IDLE
        self._params = params

        self._md = Motor(controller = self._motorcontroller,
                         params = self._params.motors.by_name("door"),
                         safety = safety)
        scale_cfg = getattr(self._params, "scale", None)
        self._scale_enabled = scale_cfg is None or getattr(scale_cfg, "enabled", True)
        self._scale = Scale(scalecontroller) if self._scale_enabled is True else None
        self._scale_ready = False
        self._logger = logging.getLogger(__name__).getChild("IOBox")
        self._curtain_sensor: Optional[CurtainSensor] = None
        self._curtain_monitor: Optional[CurtainMonitor] = None
        self._curtain_state: Optional[bool] = None
        self._curtain_listener = None
        self._door_open: Optional[bool] = None
        self._door_last_read_at: Optional[float] = None
        self._door_min_refresh_s = 0.2
        curtain_cfg = getattr(self._params, "curtain", None)
        self._door_open_threshold_mm: float = getattr(
            curtain_cfg, "door_open_threshold_mm", 5.0
        )
        self._door_debounce_s: float = getattr(curtain_cfg, "door_debounce_s", 0.0)
        self._door_candidate_state: Optional[bool] = None
        self._door_candidate_since: Optional[float] = None
        self._door_force_refresh_on_block: bool = False
        self._door_bad_reading_count: int = 0
        self._door_max_bad_readings: int = 5
        self._door_max_position_mm: float = 1000.0


    async def configure(self):
        """
        Configure and initialize IOBox components.

        Raises:
            IOBoxError: If configuration of any component fails
        """
        try:
            await self._md.configure()
            await self._md.home()
            if self._scale_enabled is True:
                self._scale_ready = True
            else:
                self._logger.info("Scale disabled via config")
            self._status = IOBoxStatus.IDLE
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"IOBox configuration failed: {str(e)}") from e

        await self.home_door()
        await self.refresh_door_state()
        await self._configure_curtain()

    async def _configure_curtain(self) -> None:
        curtain_cfg = getattr(self._params, "curtain", None)
        if curtain_cfg is None:
            self._logger.info("Curtain sensor configuration missing; skipping setup")
            return

        if not curtain_cfg.enabled:
            self._logger.info("Curtain sensor disabled via config")
            return

        pin = curtain_cfg.pin
        poll_interval = curtain_cfg.poll_interval_s

        self._curtain_sensor = CurtainSensor(pin=pin)
        self._curtain_monitor = CurtainMonitor(
            self._curtain_sensor,
            poll_interval=poll_interval,
            on_change=self._handle_curtain_change,
        )
        await self._curtain_monitor.start()
        self._logger.info(
            "Curtain monitor started",
            extra={"pin": pin, "poll_interval_s": poll_interval},
        )

    async def _ensure_scale_ready(self) -> None:
        """Ensure the scale interface is configured before reading."""
        if self._scale_enabled is False:
            return
        if self._scale_ready is True:
            return
        await self._scale.configure()
        self._scale_ready = True

    def print_random_barcode(
        self,
        *,
        height: int = 96,
        width: int = 3,
        hri: str = "none",
        feed_after: int = 2,
    ) -> str:
        """Generate and print a random UPC-A code using the USB printer."""
        if self._printer is None:
            raise IOBoxError("Printer not configured")
        try:
            return self._printer.print_random_upca(
                height=height,
                width=width,
                hri=hri,
                feed_after=feed_after,
            )
        except Exception as exc:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"Failed to print barcode: {str(exc)}") from exc

    async def get_weight(self) -> Optional[float]:
        """
        Get stable weight reading from scale in grams.
        Returns None if scale fails or is disabled.
        """
        if self._scale_enabled is False:
            return None
        start_time = time.monotonic()
        try:
            await self._ensure_scale_ready()
            weight = await self._scale.wait_for_stable_weight(timeout=15.0, poll_interval=0.1)
            elapsed = time.monotonic() - start_time
            if weight is None:
                self._logger.warning(
                    "Scale did not stabilize within timeout",
                    extra={"elapsed_s": round(elapsed, 3), "timeout_s": 15.0},
                )
            else:
                self._logger.info(
                    "Scale stabilized",
                    extra={"elapsed_s": round(elapsed, 3), "weight_grams": weight},
                )
            return weight
        except Exception as e:
            elapsed = time.monotonic() - start_time
            self._logger.error(
                "Scale read failed after %.3fs: %s", elapsed, e, extra={"elapsed_s": round(elapsed, 3)}
            )
            sentry_sdk.capture_exception(e)
            return None
    
    async def open_door(self):
        """
        Open IOBox door.
        
        Raises:
            IOBoxError: If door motor is not configured
            MotorError: If door movement fails
        """
        try:
            if self._md is None:
                raise IOBoxError("Door motor not configured")
            if self._curtain_state:
                raise IOBoxError("Curtain is blocked; refusing to open door")
            await self._md.move_to_position(self._params.motors.by_name("door").lim)  # open position
            await self.refresh_door_state(force=True)
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            if isinstance(e, IOBoxError):
                raise
            raise MotorError(f"Failed to open door: {str(e)}") from e

    async def close_door(self):
        """
        Close IOBox door.
        
        Raises:
            IOBoxError: If door motor is not configured
            MotorError: If door movement fails
        """
        try:
            if self._md is None:
                raise IOBoxError("Door motor not configured")
            await self._md.move_to_position(0)  # closed position
            await self.refresh_door_state(force=True)
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            if isinstance(e, IOBoxError):
                raise
            raise MotorError(f"Failed to close door: {str(e)}") from e

    async def home_door(self) -> None:
        """
        Home the door motor, forcing the door closed first when curtain is clear.
        """
        try:
            if self._md is None:
                raise IOBoxError("Door motor not configured")
            if not self._curtain_state:
                await self.close_door()
            await self._md.home()
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            if isinstance(e, IOBoxError):
                raise
            raise MotorError(f"Failed to home door: {str(e)}") from e

    # curtain worker?

    def _handle_curtain_change(self, state: Optional[bool]) -> None:
        previous = self._curtain_state
        self._curtain_state = state
        # Force a fresh door read when curtain blocks to avoid stale inference.
        if state:
            self._door_force_refresh_on_block = True
        self._logger.debug(
            "Curtain state change",
            extra={
                "blocked": state,
                "previous": previous,
                "force_door_refresh": bool(state),
            },
        )
        if self._curtain_listener:
            try:
                self._curtain_listener(state)
            except Exception as exc:
                self._logger.warning("Curtain listener failed: %s", exc)

    def get_curtain_state(self) -> Optional[bool]:
        return self._curtain_state

    def get_door_state(self) -> Optional[bool]:
        return self._door_open

    async def refresh_door_state(self, *, force: bool = False) -> Optional[bool]:
        """
        Best-effort inference of door state from motor position.
        Returns the latest cached state if the read fails.
        """
        if self._md is None:
            return self._door_open
        if self._door_force_refresh_on_block:
            self._logger.debug("Forcing door refresh after curtain block")
            force = True
            self._door_force_refresh_on_block = False
        now = time.monotonic()
        if not force and self._door_last_read_at is not None:
            if now - self._door_last_read_at < self._door_min_refresh_s:
                return self._door_open
        try:
            position_mm = await self._md.get_position()
        except Exception as exc:
            self._logger.debug("Door position read failed: %s", exc)
            return self._door_open

        if position_mm is None:
            self._logger.debug(
                "Door position read returned None; using cached state",
                extra={"cached_open": self._door_open},
            )
            return self._door_open

        door_limit = self._params.motors.by_name("door").lim
        is_out_of_range = (
            position_mm < 0
            or position_mm > door_limit * 1.5
            or position_mm > self._door_max_position_mm
        )

        if is_out_of_range:
            self._door_bad_reading_count += 1
            self._logger.warning(
                "Door position out of range; using cached state",
                extra={
                    "position_mm": position_mm,
                    "door_limit": door_limit,
                    "cached_open": self._door_open,
                    "bad_reading_count": self._door_bad_reading_count,
                },
            )

            if self._door_bad_reading_count >= self._door_max_bad_readings:
                self._logger.error(
                    "Too many bad door readings; attempting door motor recovery",
                    extra={"bad_reading_count": self._door_bad_reading_count},
                )
                self._door_bad_reading_count = 0
                try:
                    await self._md.home()
                    self._logger.info("Door motor homed successfully after bad readings")
                except Exception as home_exc:
                    self._logger.error("Door motor recovery failed: %s", home_exc)

            return self._door_open

        self._door_bad_reading_count = 0
        threshold = self._door_open_threshold_mm
        inferred = position_mm > threshold

        if inferred == self._door_open:
            self._door_candidate_state = None
            self._door_candidate_since = None
            self._door_last_read_at = now
            return self._door_open

        if self._door_debounce_s <= 0:
            self._door_open = inferred
            self._door_last_read_at = now
            self._logger.debug(
                "Door state inferred",
                extra={"position_mm": position_mm, "open": inferred, "threshold_mm": threshold},
            )
            return self._door_open

        if self._door_candidate_state != inferred:
            self._door_candidate_state = inferred
            self._door_candidate_since = now
            return self._door_open

        assert self._door_candidate_since is not None
        if now - self._door_candidate_since >= self._door_debounce_s:
            self._door_open = inferred
            self._door_last_read_at = now
            self._door_candidate_state = None
            self._door_candidate_since = None
            self._logger.debug(
                "Door state inferred",
                extra={
                    "position_mm": position_mm,
                    "open": inferred,
                    "threshold_mm": threshold,
                    "debounce_s": self._door_debounce_s,
                },
            )
        return self._door_open

    def set_curtain_listener(self, listener) -> None:
        self._curtain_listener = listener
        if self._curtain_state is not None and self._curtain_listener:
            try:
                self._curtain_listener(self._curtain_state)
            except Exception as exc:
                self._logger.warning("Curtain listener failed: %s", exc)

    # shutdown/teardown
    async def stop(self) -> None:
        """
        Stop all IOBox components safely.
        
        Note: Attempts best-effort cleanup of all components,
        continues even if individual stops fail.
        """
        errors = []
        
        if self._md is not None:
            try:
                await self._md.stop()
            except Exception as e:
                errors.append(f"Door motor stop failed: {str(e)}")

        if self._curtain_monitor is not None:
            try:
                await self._curtain_monitor.stop()
            except Exception as e:
                errors.append(f"Curtain monitor stop failed: {str(e)}")

        if self._curtain_sensor is not None:
            try:
                self._curtain_sensor.cleanup()
            except Exception:
                pass
        
        if errors:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError("Stop errors: " + "; ".join(errors))  
