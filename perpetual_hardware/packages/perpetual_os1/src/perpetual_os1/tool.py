import common as c

from hardware.controller import SharedModbusController as Controller
from hardware.motor import Motor, Servo, MotorStatus
from hardware.toolcam import Camera

import time
import asyncio
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Union, Literal, Annotated, List

@dataclass
class FiducialRegistry:
    # store world positions (motor coordinates) to avoid double counting
    items: List[float] = field(default_factory=list)

    def seen(self, yw: float, tol: float) -> bool:
        for yy in self.items:
            if abs(yy - yw) <= tol:
                return True
        return False

    def add(self, yw: float):
        self.items.append(yw)
 

class ToolMotionStatus(Enum):
    IDLE = 0
    MOVING = 1
    ERROR = 2

class ToolItemStatus(Enum):
    EMPTY = 0
    LOADED = 1
    ERROR = 2

class ToolStatus:
    motion: ToolMotionStatus
    item: ToolItemStatus

class Tool:
    def __init__(self, motorcontroller: Controller, params: dict):
        self._motorcontroller = motorcontroller
        self._status = ToolStatus.IDLE
        self._params = params

        self._mx = Motor(self._motorbus, self._params.get("motors",{}.get("x_axis", {})))
        self._my = Motor(self._motorbus, self._params.get("motors",{}.get("y_axis", {})))
        self._mg = Motor(self._motorbus, self._params.get("motors",{}.get("z_axis", {})))
        self._ms = Motor(self._motorbus, self._params.get("motors",{}.get("slide", {})))
        self._mr = Motor(self._motorbus, self._params.get("motors",{}.get("rotate", {})))
        self._gs = Servo(self._motorbus)

        self._cam = Camera(self._params.get("camera", {}))

        # LEDs

## MOTORS
    async def configure(self):
        await self._my.configure()
        await self._mx.configure()
        await self._mg.configure()
        await self._ms.configure()
        await self._mr.configure()
        await self.home()

    async def get_param(self, param: str):
        return self._params.get("parameters", {}).get(param, None)

    async def home(self) -> None:
        await self._ms.home()
        await self._mg.home()
        await self._mr.home()
        await self._mx.home()
        await self._my.jog(5)
        await self._my.home()

    async def clear_alarms(self) -> None:
        await self._mg.home()
        await self._ms.home()

    async def stop(self) -> None:
        await self._mx.stop()
        await self._mg.stop()
        await self._mr.stop()
        await self._ms.stop()
        await self._my.stop()

    async def move(self, coordinate: c.AxisCoordinate) -> None:
        await self._my.move_to_position_no_wait(coordinate.y)
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self.gantry_wait()

    async def retract(self) -> None:
        await self._mg.move_to_position_no_wait(0)
        await self._ms.move_to_position_no_wait(0)
        await self.tool_wait()

    async def extend(self) -> None:
        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION"))
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION"))

    async def extend_empty(self) -> None:
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION"))
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION"))
        await self.tool_wait()
    
    async def retract_empty(self) -> None:
        await self._ms.move_to_position_no_wait(0)
        await self._mg.move_to_position_no_wait(0)
        await self.tool_wait()
        
    async def pick(self, coordinate: c.AxisCoordinate) -> None:
        await self._gs.safe_open()
        await self.move(coordinate)
        await self.extend_empty()
        await self._gs.open()
        await self.retract()

    async def place(self, coordinate: c.AxisCoordinate):
        await self.move(coordinate)
        await self.extend()
        await self._gs.close()
        await self.retract_empty()

    # todo "homeable position checking"
    async def gantry_wait(self):
        while True:
            mx_f = await self._mx.wait_for_move(timeout=5)
            my_f = await self._my.wait_for_move(timeout=5)
            if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED:
                return

    async def tool_wait(self):
        while True:
            mg_f = await self._mg.wait_for_move(timeout=2.5)
            ms_f = await self._ms.wait_for_move(timeout=2.5)
            if mg_f == MotorStatus.STOPPED and ms_f == MotorStatus.STOPPED:
                return
            
    async def get_pos(self):
        # get each motor position. return tuple of axis and tool coordinate?
        x = await self._mx.get_position()
        y = await self._my.get_position()
        g = await self._mg.get_position()
        s = await self._ms.get_position()
        r = await self._mr.get_position()
        return (c.AxisCoordinate(x, y), c.ToolCoordinate(g,s,r))
    
    ### CAMERA

    async def get_shelves(self):
        shelves = []
        reg = FiducialRegistry()
        await self.move(c.AxisCoordinate(self.get_param("SCAN_POSITION")[0], 
                                         self.get_param("SCAN_POSITION")[1]))
        
        while True:
            frame = await self._cam.capture() # TODO error handling
            frame = self._cam.correct(frame)
            rect = self._cam.detect_white_squares(frame)

            # correction TODO clean up
            if rect is not None:
                _, dy = self._cam.get_center(frame, rect)
                if dy > 0:
                    await self._my.jog(dy * self.get_param("PYMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    if rect is not None:
                        _, dy = self._cam.get_center(frame, rect)
                        if dy is not None:
                            await self._my.jog(dy * self.get_param("PYMM"))
                            yw = round(await self._my.get_position(), 4)
                            if not reg.seen(yw, 20):
                                reg.add(yw)
                                shelves.append(yw)                    

            end_y = await self._my.get_position()
            lim = self._params.get("motors",{}).get("y_axis", {}).get("lim", 1000)
            if end_y + self.get_param("JOG_Y") > lim:
                break
            await self._my.jog(self.get_param("JOG_Y"))
        return shelves

    async def update_grid(self, shelves: List[float]):
        anchors = []
        await self._move(c.AxisCoordinate(self.get_param("SCAN_POSITION")[0], 
                                          self.get_param("SCAN_POSITION")[1]))
        
        for s in shelves:
            await self._my.move_to_position(s)
            frame = await self._cam.capture() # TODO error handling
            frame = self._cam.correct(frame)
            _, width = frame.shape[:2]
            third = width // 3
            frame = frame[:, third:2 * third]
            rect = self._cam.detect_white_squares(frame)
            if rect is not None:
                dx, _ = self._cam.get_center(frame, rect)
                if dx is not None:
                    await self._mx.jog(dx * self.get_param("PXMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    _, width = frame.shape[:2]
                    third = width // 3
                    frame = frame[:, third:2 * third]
                    rect = self._cam.detect_white_squares(frame)
                    if rect is not None:
                        dx, _ = self._cam.get_center(frame, rect)
                        if dx is not None:
                            await self._mx.jog(dx * self.get_param("PXMM"))
                            xw = round(await self._mx.get_position(), 4)
                            anchors.append(xw)                    
        return anchors
        

    async def auto_cal(self):
        shelves = await self.get_shelves()
        anchors = await self.update_grid(shelves)
        return anchors
        # todo eventually make this auto generate the slope of each shelf by having two scan positions in x
