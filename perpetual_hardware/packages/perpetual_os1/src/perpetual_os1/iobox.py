import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor
from hardware.iocam import AFSCam
# from hardware.scale import Scale
# from hardware.curtain import Curtain

import time
import asyncio
from enum import Enum
from typing import Dict, Optional, Tuple, Union, Literal, Annotated
from pydantic import BaseModel


class IOBoxStatus(Enum):
    IDLE = 0
    RUNNING = 1
    READY = 2
    ERROR = 3

class ItemSizeLocation(BaseModel):
    object_size: float
    min_bound: float
    max_bound: float
    height: float 

class SizeResult():
    result: Union[Literal["detected"], Literal["no_item_detected"], Literal["item_outside_of_zone"]]
    # size: Optional[ItemSizeLocation]
    # 

class IOBox:
    def __init__(self, motorcontroller: Controller, params: dict):
        # self._scalecontroller = scalecontroller
        self._motorcontroller = motorcontroller
        self._status = IOBoxStatus.IDLE
        self._params = params

        # self._scale: Optional[Scale] = None
        self._md = Motor(controller = self._motorcontroller, 
                         params = self._params.motors.by_name("door"))
        self._cam = AFSCam(usb_params = self._params.usb_camera,
                           tof_params = self._params.tof_camera)

    async def configure(self):
        await self._md.configure()
        await self._md.home()
        await self._cam.start()

        #scale config
        #curtain config

    async def get_weight(self) -> float:
        if self._scale is None:
            # self._scale = Scale(controller=self._scalecontroller, rs.gete_params(self._state, "iobox"))
            await self._scale.configure()
        weight = await self._scale.get_weight()
        return weight
    
    async def measure_item(self) -> Optional[ItemSizeLocation]:
        if self._cam is None:
            raise RuntimeError("Camera not configured")
        size = await self._cam.measure()
        return size

    async def open_door(self):
        if self._md is None:
            raise RuntimeError("Motor not configured")
        await self._md.move_to_position(220)  # open position

    async def close_door(self):
        if self._md is None:
            raise RuntimeError("Motor not configured")
        await self._md.move_to_position(0)  # closed position

    # curtain worker?

    # shutdown/teardown
    async def stop(self) -> None:
        if self._md is not None:
            await self._md.stop()
        if self._scale is not None:
            await self._scale.stop()
        if self._cam is not None:
            await self._cam.stop()  