import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor
from hardware.iocam import AFSCam
from perpetual_os1.exceptions import (
    IOBoxError, MotorError, CameraError, ConfigError
)
# from hardware.scale import Scale
# from hardware.curtain import Curtain

import time
import asyncio
from enum import Enum
from typing import Dict, Optional, Tuple, Union, Literal, Annotated
from pydantic import BaseModel

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
    def __init__(self, motorcontroller: Controller, params: dict):
        # self._scalecontroller = scalecontroller
        self._scale = None
        self._motorcontroller = motorcontroller
        self._status = IOBoxStatus.IDLE
        self._params = params

        # self._scale: Optional[Scale] = None
        self._md = Motor(controller = self._motorcontroller, 
                         params = self._params.motors.by_name("door"))
        self._cam = AFSCam(usb_params = self._params.usb_camera,
                           tof_params = self._params.tof_camera)

    async def configure(self):
        """
        Configure and initialize IOBox components.
        
        Raises:
            IOBoxError: If configuration of any component fails
        """
        try:
            await self._md.configure()
            await self._md.home()
            await self._cam.start()
            self._status = IOBoxStatus.READY
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError(f"IOBox configuration failed: {str(e)}") from e

        #scale config
        #curtain config

    async def get_weight(self) -> float:
        """
        Get weight reading from scale.
        
        Raises:
            IOBoxError: If scale is not configured or reading fails
        """
        try:
            if self._scale is None:
                # self._scale = Scale(controller=self._scalecontroller, rs.get _params(self._state, "iobox"))
                await self._scale.configure()
            weight = await self._scale.get_weight()
            return weight
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
            size = await self._cam.measure()
            return size
        except Exception as e:
            self._status = IOBoxStatus.ERROR
            if isinstance(e, IOBoxError):
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
        
        if errors:
            self._status = IOBoxStatus.ERROR
            raise IOBoxError("Stop errors: " + "; ".join(errors))  
