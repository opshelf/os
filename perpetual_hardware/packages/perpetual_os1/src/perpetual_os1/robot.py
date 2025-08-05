from enum import Enum
from pydantic import BaseModel, Field
from perpetual_os1.configuration import motor_x, motor_stage, motor_gripper, motor_rotate
from perpetual_os1.common import AxisCoordinate, ToolCoordinate
from hardware.ModbusController import SharedModbusController as Controller
from hardware.MotorBase import Motor, Servo
from hardware.camera import Camera, VideoStream
import cv2
import numpy as np
from perpetual_os1.storage import Cell, StorageGrid, StorageModuleCellLocation, CellItem, StorageModuleSide
from typing import Optional
from typing import Union, Annotated, Literal

import asyncio
SLIDE_EXTENSION = 50
GRIPPER_EXTENSION_PICK = 167
GRIPPER_EXTENSION_PLACE = 163
REVERSE = 180.5
JOG = 30

PXMM = 0.22 #pixels per mm mvmt

BAY1 = 18
BAY2 = 174
BAY3 = 330
# BAY4 = 486
BAY4 = 463.5
BAY5 = 574.5

class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"

class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"

class HomeCommand(BaseModel):
    command: Literal["home"] = "home"

class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    coordinate: AxisCoordinate

class MoveToolCommand(BaseModel):
    command: Literal["movetool"] = "movetool"
    coordinate: ToolCoordinate

class IdleCommand(BaseModel):
    command: Literal["idle"] = "idle"

RobotCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        MoveToolCommand,
        IdleCommand,
    ],
    Field(discriminator="command"),
]

# Define response types
class RobotCommandSuccessful(BaseModel):
    message: Literal["success"] = "success"


class RobotCommandError(BaseModel):
    error: str

class RobotItemStoredResponse(BaseModel):
    message: Literal["success"] = "success"
    location: StorageModuleCellLocation

class ItemSizeLocation(BaseModel):
    object_size: float
    min_bound: float
    max_bound: float
    height: float 

class SizeResult():
    result: Union[Literal["detected"], Literal["no_item_detected"], Literal["item_outside_of_zone"]]
    size: Optional[ItemSizeLocation]

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, SizeResult]

class RobotState(BaseModel):
    current_cell: Optional[StorageModuleCellLocation] = None

class RobotStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    ERROR = "ERROR"

class WorkerStatus(BaseModel):
    robot_status: Optional[RobotStatus]
    running: bool
    current_command: Optional[RobotCommands]

# TODO: MORE DICTS

PoE = "http://10.254.239.102/" # todo get this from the arp scan/nmap scan

class Robot:
    def __init__(self, port: str) -> None:
        self._bus = Controller(port=port)
        #TODO list of motors
        self._mx = Motor(self._bus, motor_x)
        self._mg = Motor(self._bus, motor_gripper)
        self._mr = Motor(self._bus, motor_rotate)
        self._ms = Motor(self._bus, motor_stage)
        self._gs = Servo(self._bus)
        self._status = RobotStatus.IDLE
        self._cam = Camera(PoE)
        self._bays = []

    async def configure(self):
        await self._bus.connect()
        if not self._bus.client.connected:
            print("aw jeez no connection")
            return
        await self._mx.configure()
        await self._mg.configure()
        await self._mr.configure()
        await self._ms.configure()
        await self._gs.configure()

        #init video stream
        # self._vstrm = VideoStream(self._cam, self._cam._vid)
        # ret, frame = self._vstrm.read() 
        # print(frame)
        # print(ret)
        # todo motor list

    async def move(self, coordinate: AxisCoordinate) -> None:
        await self.home()
        await self._mr.move_to_position(REVERSE)

        # pos = await self.auto_cal()
        # pos = 0
        end = 0
        while True: # get from params
            # print(end)
            pos = await self.auto_cal()
            print(pos)
            if pos:
                self._bays.append(pos)
                print(pos)
                await asyncio.sleep(1)
                await self._mx.jog(JOG)
            await asyncio.sleep(1)

            if end > 600:
                break

            end = await self._mx.get_position()
            end = self._mx._params["mpr"]*end/10000
        
        print(self._bays)


        # pos = await self._mx.get_position()
        # print(self._mx._params["mpr"]*pos/10000)
        # await self._mx.jog(1)
        # await self.auto_cal() # this second one should not move at all
        # self._cam.capture_sync("beb.jpg")
        # print("it's a beb")
        # await self.home()
        # await self._mr.move_to_position(REVERSE)
        # for i in range(0,100):
        #     print(i)
        #     await self.pick(AxisCoordinate(BAY4, 0))
        #     await self.place(AxisCoordinate(BAY5, 0))
        #     await self.pick(AxisCoordinate(BAY5, 0))
        #     await self.place(AxisCoordinate(BAY4, 0))

        #     await self.pick(AxisCoordinate(BAY1, 0))
        #     await self.place(AxisCoordinate(BAY2, 0))
        #     await self.pick(AxisCoordinate(BAY2, 0))
        #     await self.place(AxisCoordinate(BAY3, 0))
        #     await self.pick(AxisCoordinate(BAY3, 0))
        #     await self.place(AxisCoordinate(BAY1, 0))
            # await self.place(AxisCoordinate(BAY4, 0))
            # await self.pick(AxisCoordinate(BAY4, 0))
            # await self.place(AxisCoordinate(BAY1, 0))

    async def pick(self, coordinate: AxisCoordinate) -> None:
        await self._gs.safe_open()
        await self._mx.move_to_position(coordinate.x)
        await self._ms.move_to_position(SLIDE_EXTENSION) # 50 max
        await self._mg.move_to_position(GRIPPER_EXTENSION_PICK)
        await self._gs.open()

        await self._mg.retract()
        await self._ms.retract()

    async def place(self, coordinate: AxisCoordinate):
        await self._mx.move_to_position(coordinate.x)
        await self._ms.move_to_position(SLIDE_EXTENSION) # 50 max
        await self._mg.move_to_position(GRIPPER_EXTENSION_PLACE)
        await self._gs.close()

        await self._mg.retract()
        await self._ms.retract()

    async def home(self) -> None:
        await self._ms.home()
        await self._mg.home()
        await self._mr.home()
        await self._mx.home()

    async def stop(self) -> None:
        await self._mx.stop()
        await self._mg.stop()
        await self._mr.stop()
        await self._ms.stop()

    def get_pos(self):
        # get each motor position. return tuple of axis and tool coordinate?
        x = self._mx.get_position()
        g = self._mg.get_position()
        s = self._ms.get_position()
        r = self._mr.get_position()
        return (AxisCoordinate(x, 0), ToolCoordinate(g,s,r))
    

    async def centering(self, frame):
        frame = cv2.rotate(frame, cv2.ROTATE_180)
        height, width = frame.shape[:2]
        third = width // 3
        third_h = height // 3

        # Crop to middle third horizontally
        frame_middle = frame[:, third:2 * third]
        # Crop to bottom half vertically
        # frame_middle = frame_middle[height // 3:, :]
        frame_middle = frame_middle[2 * third_h:, :]
        cv2.imwrite("beb.jpg",frame_middle)

        bbox_crop = self._cam.get_fiducial(frame_middle)

        if bbox_crop is None:
            # print("No white rectangular fiducial detected.")
            return False
        else:
            x_crop, y, w, h = bbox_crop
            x = x_crop + third  # Shift X to full-frame coordinates
            bbox_full = (x, y, w, h)
            x = x_crop + third  # Shift X to full-frame coordinates
            score, obj_center, frame_center = self._cam.get_center(frame_middle, bbox_crop)
            # print(f"Centered score (0 = perfect): {score:.3f}")
            # print(f"Object center: {obj_center}")
            # print(f"Frame center: {frame_center}")

            # Draw bounding box and centers
            x, y, w, h = bbox_crop
            cv2.rectangle(frame_middle, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.circle(frame_middle, (int(obj_center[0]), int(obj_center[1])), 5, (255, 0, 0), -1)
            cv2.circle(frame_middle, (int(frame_center[0]), int(frame_center[1])), 5, (0, 0, 255), -1)
            offset_mm = (obj_center[0]-frame_center[0])*PXMM

            

            # print(offset_mm)

            return offset_mm

    async def auto_cal(self):
        # self._cam.start_stream()

        frame = await self._cam.capture()
        offset = await self.centering(frame)
        if offset is False:
            await self._mx.jog(JOG)
            return offset
        else:
            await self._mx.jog(-offset) # camera is upside down
            # goal_pos = self._mx._cur_pos - offset
            pos = await self._mx.get_position()
            pos = ((self._mx._params["mpr"]*pos)/10000) #in mm
            # await self._mx.jog(25) # bump it
            return pos
       

    def get_storage(self) -> StorageGrid:
        return self._storage

    def set_storage(self, value: StorageGrid) -> None:
        self._storage = value

    # async def idle(self) -> None:
    #     await self.move(self._configuration.idle_position) # basically anywhere along the bottom of the gantry

    async def shutdown(self) -> None:
        if self._bus:
            await self._bus.close()

    async def handle_command(self, command: RobotCommands) -> RobotResponse:
        match command:
            case StopCommand():
                await self.stop()
                return RobotCommandSuccessful()
            case InitializeCommand():
                await self.configure()
                return RobotCommandSuccessful()
            case HomeCommand():
                await self.home()
                return RobotCommandSuccessful()
            case MoveCommand(coordinate=coord):
                await self.move(coord)
            # case IdleCommand():
            #     await self.idle()
            #     return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
