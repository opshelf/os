import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor
from hardware.printer import USBPrinter, PrinterError
from hardware.iocam import AFSCam
from perpetual_os1.exceptions import (
    IOBoxError, MotorError, CameraError, ConfigError
)
from hardware.scale import Scale
# from hardware.curtain import Curtain

import time
import asyncio
import random
from enum import Enum
from typing import Dict, Optional, Tuple, Union, Literal, Annotated
from pydantic import BaseModel
import logging

class IOBoxStatus(Enum):
    IDLE = 0
    READY = 1
    ERROR = 2

class ItemSizeLocation(BaseModel):
    object_size: float
    min_bound: float
    max_bound: float
    height: float 

class SizeResult():
    result: Union[Literal["detected"], Literal["no_item_detected"], Literal["item_outside_of_zone"]]
    size: Optional[ItemSizeLocation]

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
        self._cam = AFSCam(usb_params = self._params.usb_camera,
                           tof_params = self._params.tof_camera)
        
        self._scale = Scale(scalecontroller)
        self._printer = USBPrinter()
        self._scale_ready = False
        self._cam_running = False
        self._logger = logging.getLogger(__name__).getChild("IOBox")

        # printer_params = getattr(self._params, "printer", None)
        # self._printer = USBPrinter(
        #     device_path=getattr(printer_params, "device_path", "/dev/usb/lp0"),
        #     encoding=getattr(printer_params, "encoding", "utf-8"),
        #     default_trailing_spaces=getattr(printer_params, "trailing_spaces", 0),
        #     default_trailing_newlines=getattr(printer_params, "trailing_newlines", 0),
        # )

    async def configure(self):
        """
        Configure and initialize IOBox components.
        
        Raises:
            IOBoxError: If configuration of any component fails
        """
        try:
            await self._md.configure()
            await self._md.home()
            # await self._scale.connect()
            await self._scale.tare()
            self._scale_ready = True
            if not self._cam_running:
                await self._cam.start()
                self._cam_running = True
            self._printer.connect()
            self._status = IOBoxStatus.IDLE
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"IOBox configuration failed: {str(e)}") from e

        #curtain config

    async def _ensure_scale_ready(self) -> None:
        """Ensure the scale interface is configured before reading."""
        if self._scale_ready:
            return
        await self._scale.configure()
        self._scale_ready = True

    async def _ensure_camera_running(self) -> None:
        """Ensure the AFSCam pipeline is active before measuring."""
        if self._cam_running:
            return
        await self._cam.start()
        self._cam_running = True

    def _generate_random_upca(self) -> str:
        digits = [str(random.randint(0, 9)) for _ in range(11)]
        odd_sum = sum(int(digits[i]) for i in range(0, 11, 2))
        even_sum = sum(int(digits[i]) for i in range(1, 11, 2))
        total = odd_sum * 3 + even_sum
        check_digit = (10 - (total % 10)) % 10
        return "".join(digits) + str(check_digit)

    def print_random_barcode(self) -> str:
        """Generate and print a random UPC-A code using the USB printer."""
        if self._printer is None:
            raise IOBoxError("Printer not configured")
        upca = self._generate_random_upca()
        try:
            self._printer.print_text(f"UPC-A: {upca}")
            return upca
        except PrinterError as exc:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"Failed to print barcode: {str(exc)}") from exc

    async def get_weight(self) -> float:
        """
        Get weight reading from scale.
        
        Raises:
            IOBoxError: If scale is not configured or reading fails
        """
        try:
            await self._ensure_scale_ready()
            retries = 2
            for attempt in range(retries):
                try:
                    return await self._scale.get_weight()
                except Exception as err:
                    self._logger.warning(
                        "Scale read failed (attempt %s/%s)",
                        attempt + 1,
                        retries,
                        exc_info=err,
                    )
                    if attempt == retries - 1:
                        raise
                    await asyncio.sleep(0.05)
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"Failed to get weight: {str(e)}") from e
    
    async def measure_item(self) -> Optional[ItemSizeLocation]:
        """
        Measure item dimensions using camera system.
        
        Raises:
            IOBoxError: If camera is not configured
            CameraError: If measurement fails
        """
        try:
            if self._cam is None:
                raise IOBoxError("Camera not configured")
            await self._ensure_camera_running()
            size = await self._cam.measure()
            return size
        except Exception as e:
            self._logger.warning("Async measurement failed", exc_info=e)
            if isinstance(e, IOBoxError):
                self._status = IOBoxStatus.ERROR
                raise
            raise CameraError(f"Item measurement failed: {str(e)}") from e

    async def detect_item_presence(self) -> Tuple[Optional[ItemSizeLocation], float]:
        """
        Determine whether an item is present by combining scale weight and camera measurement.

        Returns:
            Tuple containing the measured ItemSizeLocation (or None) and the recorded weight.

        Side Effects:
            Updates IOBox status to READY when an item is confirmed present, otherwise IDLE.
        """
        try:
            weight = await self.get_weight()
            measurement = await self.measure_item()

            if measurement is not None and weight != 0:
                self._status = IOBoxStatus.READY
            else:
                self._status = IOBoxStatus.IDLE

            return measurement, weight
        except Exception:
            # get_weight / measure_item already set status/errors appropriately
            raise
        
    def measure_item_sync(self) -> Optional[ItemSizeLocation]:
        """
        Measure item dimensions using camera system.
        
        Raises:
            IOBoxError: If camera is not configured
            CameraError: If measurement fails
        """
        try:
            if self._cam is None:
                raise IOBoxError("Camera not configured")
            size = self._cam.measure_sync()
            return size
        except Exception as e:
            self._logger.warning("Sync measurement failed", exc_info=e)
            if isinstance(e, IOBoxError):
                self._status = IOBoxStatus.ERROR
                raise
            raise CameraError(f"Item measurement failed: {str(e)}") from e

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

    def print_text(self, text: str, *, trailing_spaces: Optional[int] = None, trailing_newlines: Optional[int] = None) -> None:
        """
        Print text using the IOBox printer.

        Raises:
            IOBoxError: If the printer is not available or the write fails.
        """
        if self._printer is None:
            raise IOBoxError("Printer not configured")
        try:
            self._printer.print_text(
                text,
                trailing_spaces=trailing_spaces,
                trailing_newlines=trailing_newlines,
            )
        except PrinterError as exc:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"Printer failed: {str(exc)}") from exc

    # curtain worker?

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
                
        if self._scale is not None:
            try:
                await self._scale.stop()
            except Exception as e:
                errors.append(f"Scale stop failed: {str(e)}")
                
        if self._cam is not None:
            try:
                await self._cam.stop()
            except Exception as e:
                errors.append(f"Camera stop failed: {str(e)}")

        if self._printer is not None:
            try:
                self._printer.disconnect()
            except Exception as e:
                errors.append(f"Printer disconnect failed: {str(e)}")
        
        if errors:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError("Stop errors: " + "; ".join(errors))  
