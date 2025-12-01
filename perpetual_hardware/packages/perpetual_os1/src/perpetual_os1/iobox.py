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


class IOBoxStatus(Enum):
    IDLE = 0
    READY = 1
    ERROR = 2

class IOBox:
    def __init__(self, 
                 motorcontroller: Controller,
                 scalecontroller: Controller, 
                 params: dict,
                 ):
        self._scalecontroller = scalecontroller
        self._motorcontroller = motorcontroller
        self._status = IOBoxStatus.IDLE
        self._params = params

        self._md = Motor(controller = self._motorcontroller,
                         params = self._params.motors.by_name("door"))
        self._scale = Scale(scalecontroller)
        self._scale_ready = False
        self._logger = logging.getLogger(__name__).getChild("IOBox")
        self._curtain_sensor: Optional[CurtainSensor] = None
        self._curtain_monitor: Optional[CurtainMonitor] = None
        self._curtain_state: Optional[bool] = None
        self._curtain_listener = None


    async def configure(self):
        """
        Configure and initialize IOBox components.
        
        Raises:
            IOBoxError: If configuration of any component fails
        """
        try:
            await self._md.configure()
            await self._md.home()
            await self._scale.tare()
            self._scale_ready = True
            self._status = IOBoxStatus.IDLE
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"IOBox configuration failed: {str(e)}") from e

        await self._configure_curtain()

    async def _configure_curtain(self) -> None:
        if not getattr(self._params, "curtain_enabled", True):
            self._logger.info("Curtain sensor disabled via config")
            return

        pin = 2
        poll_interval = 0.01

        self._curtain_sensor = CurtainSensor(pin=pin)
        self._curtain_monitor = CurtainMonitor(
            self._curtain_sensor,
            poll_interval=poll_interval,
            on_change=self._handle_curtain_change,
        )
        await self._curtain_monitor.start()
        self._logger.info(
            "Curtain monitor started",
            extra={"pin": pin, "poll_interval": poll_interval},
        )

    async def _ensure_scale_ready(self) -> None:
        """Ensure the scale interface is configured before reading."""
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
        Returns None if scale fails (does not raise exception).
        """
        try:
            await self._ensure_scale_ready()
            weight = await self._scale.wait_for_stable_weight(timeout=3.0, poll_interval=0.1)
            if weight is None:
                self._logger.warning("Scale did not stabilize within timeout")
            return weight
        except Exception as e:
            self._logger.error("Scale read failed: %s", e)
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
            await self._md.move_to_position(self._params.motors.by_name("door").lim)  # open position
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
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            if isinstance(e, IOBoxError):
                raise
            raise MotorError(f"Failed to close door: {str(e)}") from e

    # curtain worker?

    async def _handle_curtain_change(self, state: Optional[bool]) -> None:
        self._curtain_state = state
        if self._curtain_listener:
            try:
                self._curtain_listener(state)
            except Exception as exc:
                self._logger.warning("Curtain listener failed: %s", exc)

    def get_curtain_state(self) -> Optional[bool]:
        return self._curtain_state

    def set_curtain_listener(self, listener) -> None:
        self._curtain_listener = listener

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
