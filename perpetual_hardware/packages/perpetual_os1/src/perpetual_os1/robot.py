from typing import Dict, Optional, Tuple, Union, Literal, Annotated
from perpetual_os1.common import AxisCoordinate, ToolCoordinate
from hardware.ModbusController import SharedModbusController as Controller
from hardware.MotorBase import Motor, Servo, MotorStatus
from hardware.camera import Camera
from hardware.scale import Scale
from hardware.curtain import Curtain
from pydantic import BaseModel, Field
import time
from enum import Enum
import cv2
import asyncio
import perpetual_os1.robot_state as rs

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

# class RobotItemStoredResponse(BaseModel):
#     message: Literal["success"] = "success"
#     location: StorageModuleCellLocation

class ItemSizeLocation(BaseModel):
    object_size: float
    min_bound: float
    max_bound: float
    height: float 

class SizeResult():
    result: Union[Literal["detected"], Literal["no_item_detected"], Literal["item_outside_of_zone"]]
    size: Optional[ItemSizeLocation]

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, SizeResult]

# class RobotState(BaseModel):
#     current_cell: Optional[StorageModuleCellLocation] = None

class RobotStatus(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    ERROR = "ERROR"

class WorkerStatus(BaseModel):
    robot_status: Optional[RobotStatus]
    running: bool
    current_command: Optional[RobotCommands]


from pathlib import Path
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

import cv2
import numpy as np
import asyncio
import csv
import time
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict

# ---------------------------
# Config
# ---------------------------
@dataclass
class ScanConfig:
    x_min: float = 0.0
    x_max: float = 300.0           # mm or steps—match your units
    y_min: float = 0.0
    y_max: float = 200.0
    x_step: float = 10.0
    y_step: float = 10.0
    center_tol_px: int = 3         # how close to perfect center is "centered"
    px_per_mm_x: float = 4.5       # camera scale: pixels per mm (x)
    px_per_mm_y: float = 4.5       # camera scale: pixels per mm (y)
    move_timeout_s: float = 3.0
    registry_tol_mm: float = 1.0   # "same fiducial" if world distance < this
    min_area_px: int = 100         # reject tiny noise
    aspect_tol: float = 0.25       # allowed deviation from 1:1 square
    nms_iou: float = 0.3           # NMS threshold for multiple contours

# ---------------------------
# Global de-dup registry
# ---------------------------
@dataclass
class FiducialRegistry:
    # store world positions (motor coordinates) to avoid double counting
    items: List[Tuple[float, float]] = field(default_factory=list)

    def seen(self, xw: float, yw: float, tol: float) -> bool:
        for xx, yy in self.items:
            if (xx - xw) ** 2 + (yy - yw) ** 2 <= tol ** 2:
                return True
        return False

    def add(self, xw: float, yw: float):
        self.items.append((xw, yw))

class Robot:
    def __init__(self, port: str) -> None:
        self._bus = Controller(port=port)
        # self._curtain_bus = Controller(port=curtain_port)
        self._state = rs.load_state(CONFIG_PATH)
        self._mx = Motor(
            self._bus, 
            rs.get_motor_params(self._state, "x_axis"), 
        )
        self._my = Motor(
            self._bus, 
            rs.get_motor_params(self._state, "y_axis"), 
        )
        self._mg = Motor(
            self._bus, 
            rs.get_motor_params(self._state, "z_axis"), 
        )
        self._ms = Motor(
            self._bus, 
            rs.get_motor_params(self._state, "stage"), 
        )
        self._mr = Motor(
            self._bus, 
            rs.get_motor_params(self._state, "rotate"), 
        )
        self._md = Motor(
            self._bus, 
            rs.get_motor_params(self._state, "door"), 
        )

        self._gs = Servo(self._bus)
        self._cam = Camera()
        self._grid = self._state["grid"]
        # self._scale = Scale(self._bus, 0x1) #need to update motors
        # self._curtain = Curtain(self._curtain_bus)
        self._cur_item: str = None # some kind of unique identifier
        self._status: RobotStatus = RobotStatus.IDLE

    async def configure(self):
        await self._bus.connect()
        if not self._bus.client.connected:
            print("No Connection to Modbus")
            return
        await self._mx.configure()
        await self._my.configure()
        await self._mg.configure()
        await self._mr.configure()
        await self._ms.configure()
        await self._md.configure()
        await self._gs.configure()

        await self.home()

        # await self._scale.configure()
        # await self._curtain.connect()

    def get_param(self, param: str):
        return rs.get_robot_params(self._state).get(param)

    async def retrieve_item(self, label: str):
        item = AxisCoordinate(rs.search_grid(self.get_storage()))
        self.pick(item)

    async def place_item(self):
        # get next available position
        next = rs.find_first_available_cell(self.get_storage())
        if next is None:
            print("No available cell found.")
            return

        coordinate = AxisCoordinate(next["coord"])
        await self.place(coordinate)

    async def move_to_io(self):
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_LOCATION_L")[0])
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_LOCATION_L")[1])
        await self.gantry_wait()

    async def pick(self, coordinate: AxisCoordinate) -> None:
        await self._gs.safe_open()
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()
        #empty move
        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

    async def place(self, coordinate: AxisCoordinate):
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()
        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE"))
        await self._gs.close()

        await self._ms.move_config()
        await self._mg.move_config()
        await self._mg.move_to_position_no_wait(0)
        await self._ms.move_to_position_no_wait(0)
        await self.tool_wait()

        await self._mg.retract()
        await self._ms.retract()

    async def pickplace(self, coordinate: AxisCoordinate) -> None:
        BAY1 = AxisCoordinate(252.5, 42.5)
        # await self.get_shelves()
        # await self.update_grid()
        await self.home()
        # await self._mx.move_to_position(252.5)
        # await self._my.move_to_position(300)
        # await self.home()
        # await self.home()
        # await self.home()
        # await self.home()

        await self._mr.move_to_position(0.5)
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(BAY1.x) 
        await self._my.move_to_position_no_wait(BAY1.y)
        await self.gantry_wait()

        await self._gs.safe_open()
        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[1])
        await self.gantry_wait()

        await self._md.move_to_position(170)

        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION"))
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE")+5)

        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[1])
        await self.gantry_wait()
        await self._mg.retract()
        await self._ms.retract()

        await self._md.move_to_position(0)

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(BAY1.x) 
        await self._my.move_to_position_no_wait(BAY1.y)
        await self.gantry_wait()

        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PLACE"))
        await self.tool_wait()

        await self._gs.close()
        await self._mg.retract()
        await self._ms.retract()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(BAY1.x) 
        await self._my.move_to_position_no_wait(BAY1.y)
        await self.gantry_wait()

        await self._gs.safe_open()
        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[1])
        await self.gantry_wait()

        await self._md.move_to_position(170)

        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PLACE")+5)
        await self.tool_wait()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[1])
        await self.gantry_wait()

        await self._mg.retract()
        await self._ms.retract()

        await self._md.move_to_position(0)

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(BAY1.x) 
        await self._my.move_to_position_no_wait(BAY1.y)
        await self.gantry_wait()

        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PLACE"))
        await self.tool_wait()

        await self._gs.close()
        await self._mg.retract()
        await self._ms.retract()

    async def get_shelves(self):

        shelves = []
        
        await self.home()
        await self._mr.move_to_position(0.5)
        await self._move(AxisCoordinate(self.get_param("SCAN_POSITION")[0], 
                                        self.get_param("SCAN_POSITION")[1]))

        cap = cv2.VideoCapture(self._cam.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError("bad cam")
        
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
        
        while True:
            t0 = time.time()
            while time.time() - t0 < 4.5: # idr interval
                cap.grab()
                time.sleep(0.001)

            for _ in range(5):
                cap.grab()

            ok, frame = cap.read()
            if not ok or frame is None:
                # reconnect on failure
                cap.release()
                cap = cv2.VideoCapture(self._cam.url, cv2.CAP_FFMPEG) 
                if not cap.isOpened():
                    break
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
                continue
            
            frame = cv2.resize(frame, None, fx=1.1, fy=1, interpolation=cv2.INTER_LINEAR)
            rect = self._cam.detect_white_squares(frame)
            if rect is not None:
                _, dy = self._cam.get_center(frame, rect)
                if dy > 0:
                    await self._my.jog(dy * self.get_param("PYMM"))
                    yw = round(await self._my.get_position(), 4)

                    shelves.append(yw)
                    # print(f"yw: {yw}")

            end_y = await self._my.get_position()
            if end_y + self.get_param("JOG_Y") > rs.get_motor_params(self._state, "y_axis").get("lim"):
                break
            await self._my.jog(self.get_param("JOG_Y"))

        cap.release()
        return shelves

    async def update_grid(self, shelves: List[float]):
        await self._mr.move_to_position(0.5)
        await self._move(AxisCoordinate(self.get_param("SCAN_POSITION")[0], 1))

        cap = cv2.VideoCapture(self._cam.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError("bad cam")
        
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

        bays = []

        for yw in shelves:
            await self._my.move_to_position(yw)
            while True:
                t0 = time.time()
                while time.time() - t0 < 4.5: # idr interval
                    cap.grab()
                    # tiny sleep keeps CPU sane and lets decoder progress
                    time.sleep(0.001)

                for _ in range(5):
                    cap.grab()

                ok, frame = cap.read()
                
                if not ok:
                    continue

                if frame is None:
                    print("bad frame")
                    continue
                
                height, width = frame.shape[:2]
                third = width // 3

                frame = frame[:, third:2 * third]

                frame = cv2.resize(frame, None, fx=1.1, fy=1, interpolation=cv2.INTER_LINEAR)
                rect = self._cam.detect_white_squares(frame)
                if rect is not None:
                    dx, _ = self._cam.get_center(frame, rect)
                    # only track increasing boxes
                    if dx is not None:
                        await self._mx.jog(dx * self.get_param("PXMM"))
                        xw = round(await self._mx.get_position(), 4)
                        bays.append((xw+7, yw+127))
                        print(f"xw: {xw}, yw: {yw}")

                end_x = await self._mx.get_position()
                lim = rs.get_motor_params(self._state, "x_axis").get("lim")
                jog_x = float(self.get_param("JOG_X"))
                if end_x + jog_x > lim:
                    await self._mx.move_to_position(self.get_param("SCAN_POSITION")[0])
                    break
                await self._mx.jog(self.get_param("JOG_X"))

        print(bays)

        cap.release()

    async def _move(self, coordinate: AxisCoordinate) -> None:
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x) 
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()

        # +7 x and +127 y

    async def move(self, coordinate: AxisCoordinate) -> None:
        # shelves = await self.get_shelves()
        # await self.update_grid(shelves)
        # await self._mx.move_config()
        # await self._my.move_config()
        # await self._mx.move_to_position_no_wait(coordinate.x) 
        # await self._my.move_to_position_no_wait(coordinate.y)
        # await self.gantry_wait()
        await self.pick(AxisCoordinate(coordinate.x, coordinate.y))

    async def gantry_wait(self):
        while True:
            mx_f = await self._mx.wait_for_move(timeout=10)
            my_f = await self._my.wait_for_move(timeout=10)

            if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED:
                return

    async def tool_wait(self):
        while True:
            mg_f = await self._mg.wait_for_move(timeout=2.5)
            ms_f = await self._ms.wait_for_move(timeout=2.5)

            if mg_f == MotorStatus.STOPPED and ms_f == MotorStatus.STOPPED:
                return

    async def get_weight(self) -> float:
        try:
            data = await self._scale.read_weight_data()
            return data['weight']
        except Exception as e:
            print(f"Error reading weight: {e}")
            return 0.0

    def get_storage(self) -> Dict[str, rs.GridCell]:
        return self._grid

    def set_storage(self, grid: Dict[str, rs.GridCell]) -> None:
        self._grid = grid

    async def idle(self) -> None:
        await self.move(self.get_param("IDLE_POSITION")) # basically anywhere along the bottom of the gantry

    async def shutdown(self) -> None:
        if self._bus:
            await self._bus.close()

    #############################

    async def home(self) -> None:
        await self._ms.home()
        await self._mg.home()
        await self._mr.home()
        await self._mx.home()
        await self._my.jog(0.1)
        await self._my.home_y()
        await self._md.home()

    async def stop(self) -> None:
        await self._mx.stop()
        await self._mg.stop()
        await self._mr.stop()
        await self._ms.stop()
        await self._my.stop()

    async def get_pos(self):
        # get each motor position. return tuple of axis and tool coordinate?
        x = await self._mx.get_position()
        y = await self._my.get_position()
        g = await self._mg.get_position()
        s = await self._ms.get_position()
        r = await self._mr.get_position()
        return (AxisCoordinate(x, y), ToolCoordinate(g,s,r))
    
    async def handle_command(self, command: RobotCommands):
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
                return RobotCommandSuccessful()
            # case IdleCommand():
            #     await self.idle()
            #     return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
