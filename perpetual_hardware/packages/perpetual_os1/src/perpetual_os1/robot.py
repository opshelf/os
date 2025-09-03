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
    items: List[float] = field(default_factory=list)

    def seen(self, yw: float, tol: float) -> bool:
        for yy in self.items:
            if abs(yy - yw) <= tol:
                return True
        return False

    def add(self, yw: float):
        self.items.append(yw)

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

        # await self._md.move_to_position(170) # todo make param

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

    async def io_place(self, coord: AxisCoordinate):
        # await self._mx.move_config()
        # await self._my.move_config()
        await self.pick(coord)

        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[1])
        await self._md.move_to_position_no_wait(170)
        await self.pick_wait()

        # await self._md.move_to_position(170) # todo make para
        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION"))
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE")+8)

        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[1])
        await self.gantry_wait()
        await self._mg.retract()
        await self._ms.retract()

        await self._md.move_to_position_no_wait(0)
        await self._mx.move_to_position_no_wait(coord.x)
        await self._my.move_to_position_no_wait(coord.y)
        await self.pick_wait()

        await self.place(coord)

        await self._md.home()

    async def io_pick(self, coord):

        await self.pick(coord)
        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[1])
        await self._md.move_to_position_no_wait(170)
        await self.pick_wait()

        # await self._md.move_to_position(170)

        # await self._ms.move_config()
        # await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PLACE")+8)
        await self.tool_wait()

        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[0]) 
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[1])
        await self.gantry_wait()

        await self._mg.retract()
        await self._ms.retract()

        await self._md.move_to_position(0)
        await self._mx.move_to_position_no_wait(coord.x)
        await self._my.move_to_position_no_wait(coord.y)
        await self.pick_wait()

        await self.place(coord)

        await self._md.home()

    async def pick(self, coordinate: AxisCoordinate) -> None:
        await self._gs.safe_open()
        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()
        #empty move
        # await self._ms.move_config()
        # await self._mg.move_config()
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

        # await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION")) # 50 max
        # await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE"))
        # await self._gs.close()

        # await self._mg.retract()
        # await self._ms.retract()

        # await self._ms.move_config()
        # await self._mg.move_config()
        # await self._mg.move_to_position_no_wait(0)
        # await self._ms.move_to_position_no_wait(0)
        # await self.tool_wait()

    async def place(self, coordinate: AxisCoordinate):
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()
        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE"))
        await self._gs.close()

        # await self._ms.move_config()
        # await self._mg.move_config()
        await self._mg.move_to_position_no_wait(0)
        await self._ms.move_to_position_no_wait(0)
        await self.tool_wait()

        # await self._mg.retract()
        # await self._ms.retract()

    async def test(self) -> None:
        # bays = self._state.get("grid")
        # (244.488, 39.888), (409.72, 39.888), (572.896, 39.888), (736.6, 39.888), (903.536, 39.888),
        # bays = [ (288.08, 383.88), (381.32, 383.88), (490.08, 383.88), (594.744, 383.88), (698.8, 383.88), (804.184, 383.88), (909.424, 383.88), (288.904, 590.72), (380.984, 590.72), (490.792, 590.72), (593.464, 590.72), (697.264, 590.72), (802.912, 590.72), (909.752, 590.72), (284.416, 789.74), (385.016, 789.74), (478.712, 789.74), (566.656, 789.74), (657.624, 789.74), (745.344, 789.74), (837.664, 789.74), (927.456, 789.74), (284.872, 943.072), (385.464, 943.072), (476.704, 943.072), (566.624, 943.072), (655.792, 943.072), (747.936, 943.072), (836.976, 943.072), (927.272, 943.072)]
        # bays = [(246.616, 12.832), (412.68, 12.832), (576.016, 12.832), (739.832, 12.832), (905.984, 12.832), (280.704, 350.248), (387.0, 350.248), (492.096, 350.248), (597.736, 350.248), (701.968, 350.248), (807.496, 350.248), (912.48, 350.248), (281.768, 553.712), (387.056, 553.712), (492.072, 553.712), (596.864, 553.712), (700.328, 553.712), (805.792, 553.712), (911.84, 553.712), (289.544, 753.96), (379.56, 753.96), (470.0, 753.96), (559.84, 753.96), (649.896, 753.96), (739.28, 753.96), (829.536, 753.96), (919.888, 753.96), (289.808, 901.22), (380.152, 901.22), (469.696, 901.22), (559.624, 901.22), (649.208, 901.22), (739.832, 901.22), (830.072, 901.22), (920.072, 901.22)]
        # bays = [(283.7, 382)]
        # bays = [(288.916, 382.968)]
        bays = [(288.916, 382.968)]
                # , (395.252, 382.968), (500.932, 382.968), (606.228, 382.968), (711.332, 382.968), (816.556, 382.968), (922.34, 382.968), (289.244, 583.52), (395.564, 583.52), (501.34, 583.52), (606.068, 583.52), (710.14, 583.52), (814.692, 583.52), (920.156, 583.52), (389.564, 783.568), (480.252, 783.568), (570.404, 783.568), (660.252, 783.568), (749.172, 783.568), (840.116, 783.568), (930.652, 783.568), (390.308, 933.828), (479.588, 933.828), (569.964, 933.828), (659.644, 933.828), (750.292, 933.828), (929.476, 933.828)]
        # bays = [(283.724, 382.408), (386.988, 382.408), (491.444, 382.408), (596.14, 382.408), (707.044, 382.408), (809.372, 382.408), (913.3, 382.408)]
        for cell in bays:
            # coord = bays.get(cell).get("coord")
            bay = AxisCoordinate(round(cell[0]+2.5), round(cell[1])+2) #0.5, 3
            await self._move(bay)

            cap = cv2.VideoCapture(self._cam.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                raise RuntimeError("bad cam")
            
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

            t0 = time.time()
            while time.time() - t0 < 4.25: # idr interval
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
            
            #todo dedup
            
            # frame = cv2.resize(frame, None, fx=1.25, fy=1, interpolation=cv2.INTER_LINEAR)
            cv2.imwrite("beb.png", frame)
            # await self.pick(bay)
        

            # await self._md.move_to_position(0)
            # await self._md.home()
            # await self.place(bay)

    async def get_shelves(self):
        shelves = []
        # last_shelf = None
        reg = FiducialRegistry()
        
        # await self.home()
        await self._move(AxisCoordinate(self.get_param("SCAN_POSITION")[0], 
                                        self.get_param("SCAN_POSITION")[1]))
        
        while True:
            frame = await self._cam.capture() # TODO error handling
            frame = self._cam.correct(frame)
            #todo dedup
            # frame = cv2.resize(frame, None, fx=1.25, fy=1, interpolation=cv2.INTER_LINEAR)
            cv2.imwrite("beb.png", frame)
            rect = self._cam.detect_white_squares(frame)
            # print(rect)
            if rect is not None:
                _, dy = self._cam.get_center(frame, rect)
                if dy > 0:
                    await self._my.jog(dy * self.get_param("PYMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    cv2.imwrite("beb2.png", frame)
                    if rect is not None:
                        _, dy = self._cam.get_center(frame, rect)
                        if dy is not None:
                            await self._my.jog(dy * self.get_param("PYMM"))
                            yw = round(await self._my.get_position(), 4)
                        
                            if not reg.seen(yw, 20):
                                reg.add(yw)
                                shelves.append(yw)
                                print(yw)                          

            end_y = await self._my.get_position()
            # if end_y + self.get_param("JOG_Y") > rs.get_motor_params(self._state, "y_axis").get("lim"):
            if end_y + self.get_param("JOG_Y") > 1000:
                break
            await self._my.jog(self.get_param("JOG_Y"))
            # last_shelf = yw
        return shelves

    async def update_grid(self, shelves: List[float]):
        anchors = []
        # last_shelf = None
        reg = FiducialRegistry()
        
        # await self.home()
        await self._move(AxisCoordinate(self.get_param("SCAN_POSITION")[0], 
                                        self.get_param("SCAN_POSITION")[1]))
        
        for s in shelves:
            await self._my.move_to_position(s)
            frame = await self._cam.capture() # TODO error handling
            frame = self._cam.correct(frame)
            height, width = frame.shape[:2]
            third = width // 3
            frame = frame[:, third:2 * third]
            # cv2.imwrite("beb.png", frame)
            rect = self._cam.detect_white_squares(frame)
            # print(rect)
            if rect is not None:
                dx, _ = self._cam.get_center(frame, rect)
                # print(dx)
                if dx is not None:
                    await self._mx.jog(dx * self.get_param("PXMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    height, width = frame.shape[:2]
                    third = width // 3
                    frame = frame[:, third:2 * third]
                    # cv2.imwrite("beb2.png", beb)
                    rect = self._cam.detect_white_squares(frame)
                    if rect is not None:
                        # print(rect)
                        dx, _ = self._cam.get_center(frame, rect)
                        if dx is not None:
                            await self._mx.jog(dx * self.get_param("PXMM"))
                            xw = round(await self._mx.get_position(), 4)
                        
                            # if not reg.seen(xw, 20): # diff shelves
                            reg.add(xw)
                            anchors.append(xw)
                            print(xw)                          
        return anchors


    # TODO move video stream stuff to camera (to hide)
    async def _update_grid(self, shelves: List[float]):
        await self._move(AxisCoordinate(self.get_param("SCAN_POSITION")[0], 1))

        bays = []

        for yw in shelves:
            cap = cv2.VideoCapture(self._cam.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                raise RuntimeError("bad cam")
            
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
            await self._my.move_to_position(yw)
            t0 = time.time()
            while time.time() - t0 < 4.25: # idr interval
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

            # frame = cv2.resize(frame, None, fx=1.25, fy=1, interpolation=cv2.INTER_LINEAR)
            rect = self._cam.detect_white_squares(frame)
            if rect is not None:
                dx, dy = self._cam.get_center(frame, rect)
                # only track increasing boxes
                if dx is not None and dy is not None :
                    # await self._mx.jog(dx * self.get_param("PXMM"))
                    xw = round(await self._mx.get_position(), 4) + dx*self.get_param("PXMM")
                    y_f = round(await self._my.get_position(), 4) + dy*self.get_param("PYMM")
                    print(f"xw actual: {xw}, yw actual: {y_f}")
                    bays.append((xw, yw)) # update a grid at the end using the list

            # end_x = await self._mx.get_position()
            # lim = rs.get_motor_params(self._state, "x_axis").get("lim")
            # jog_x = float(self.get_param("JOG_X"))
            # if end_x + jog_x > lim:
            #     await self._mx.move_to_position(self.get_param("SCAN_POSITION")[0])
            #     break
            # await self._mx.jog(self.get_param("JOG_X"))

            cap.release()

        return bays

    async def _move(self, coordinate: AxisCoordinate) -> None:
        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x) 
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()

    async def move(self, coordinate: AxisCoordinate) -> None:
        # bay = AxisCoordinate(round(bays[0]+2.5), round(bays[1])+2) #0.5, 3
        await self._mr.move_to_position(0.5)
        shelves = [21.048, 372.04 ,575.44 ,776.84, 927.24]
        # # await self.auto_cal()
        anchors = [910.44, 912.344, 913.008, 921.272, 921.68]
        # # await self.update_grid(shelves)
        bays = [AxisCoordinate(anchors[0]+4, shelves[0]+15),AxisCoordinate(anchors[1]+4, shelves[1]+15),AxisCoordinate(anchors[2]+4, shelves[2]+15), AxisCoordinate(anchors[3]+4, shelves[3]+15), AxisCoordinate(anchors[4]+4, shelves[4]+15)]
        # await self._move(bays[0])
        # time.sleep(5)
        # await self._move(bays[1])
        # time.sleep(5)
        # await self._move(bays[2])
        # time.sleep(5)
        # await self._move(bays[3])
        # time.sleep(5)
        # await self._move(bays[4])
        # time.sleep(5)

        for bay in bays:
            await self.pick(bay)
            await self.place(bay)

        # # 252.5, 42.5
        # await self._move(AxisCoordinate(coordinate.x, coordinate.y))
        # beb = await self._cam.capture()
        # beb = self._cam.correct(beb)
        # cv2.imwrite("test.png", beb)
        # bebst = self._cam.detect_white_squares(beb)
        # cx, cy = self._cam.get_center(beb, bebst)
        # print(cx*0.1)
        # print(cy*0.1)
        
        # if beb is not None:
        #     print("beb")
        #     self._cam.correct(beb)
        # cv2.imwrite("1.jpg", beb)
        # await self._move(AxisCoordinate(20, 481))
        # beb = await self._cam.capture()
        # cv2.imwrite("2.jpg", beb)
        # await self._move(AxisCoordinate(20, 491))
        # beb = await self._cam.capture()
        # cv2.imwrite("3.jpg", beb)
        # await self._move(AxisCoordinate(20, 471))
        # beb = await self._cam.capture()
        # cv2.imwrite("4.jpg", beb)
        # await self._move(AxisCoordinate(10, 471))
        # beb = await self._cam.capture()
        # cv2.imwrite("5.jpg", beb)
        # await self._move(AxisCoordinate(30, 471))
        # beb = await self._cam.capture()
        # cv2.imwrite("6.jpg", beb)
        # await self._move(AxisCoordinate(10, 481))
        # beb = await self._cam.capture()
        # cv2.imwrite("7.jpg", beb)
        # await self._move(AxisCoordinate(30, 481))
        # beb = await self._cam.capture()
        # cv2.imwrite("8.jpg", beb)
        # await self._move(AxisCoordinate(10, 491))
        # beb = await self._cam.capture()
        # cv2.imwrite("9.jpg", beb)
        # await self._move(AxisCoordinate(30, 491))
        # beb = await self._cam.capture()
        # cv2.imwrite("10.jpg", beb)

        
        

    async def auto_cal(self):
        shelves = await self.get_shelves()
        print(shelves)
        # shelves_n = [item - 30 for item in shelves]
        anchors = await self.update_grid(shelves)
        print(anchors)
        # rs.build_grid_from_coords(self._state, bays, key_prefix="S", start_index=1, zero_pad=2)
        # print(self._state)

    async def pick_wait(self):
        while True:
            mx_f = await self._mx.wait_for_move(timeout=10)
            my_f = await self._my.wait_for_move(timeout=10)
            md_f = await self._md.wait_for_move(timeout=10)

            # if mx_f == MotorStatus.ERROR:
            #     await self._mx.home()

            # if my_f == MotorStatus.ERROR:
            #     await self._my.home_y()

            if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED and md_f == MotorStatus.STOPPED:
                return


    # todo "homeable position checking"
    async def gantry_wait(self):
        while True:
            mx_f = await self._mx.wait_for_move(timeout=5)
            my_f = await self._my.wait_for_move(timeout=5)

            # if mx_f == MotorStatus.ERROR:
            #     await self._mx.home()

            # if my_f == MotorStatus.ERROR:
            #     await self._my.home_y()

            if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED:
                return

    async def tool_wait(self):
        while True:
            mg_f = await self._mg.wait_for_move(timeout=2.5)
            ms_f = await self._ms.wait_for_move(timeout=2.5)

            # if mg_f == MotorStatus.ERROR:
            #     await self._mg.home()

            # if ms_f == MotorStatus.ERROR:
            #     await self._ms.home()

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
        await self._md.stop()

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
