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


# class TestCommandOne(BaseModel):
#     command: Literal["testone"] = "testone"

# class TestCommandTwo(BaseModel):
#     command: Literal["testtwo"] = "testtwo"

class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"

class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"

class HomeCommand(BaseModel):
    command: Literal["home"] = "home"
    barcode: str

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
        self._bus = Controller(port="/dev/ttyUSB0", baudrate=115200) # change to config
        self._scalebus = Controller(port="/dev/ttyACM0", baudrate=9600) # change to config
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
        # self._cam = Camera()
        # self._grid = self._state["grid"]
        self._scale = Scale(self._scalebus, 1) #need to update motors
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

        # await self._scalebus.connect()
        if not await self._scale.connect():
            print("No Connection to Scale")

        await self._scale.tare()
        # await self._curtain.connect()

    def get_param(self, param: str):
        return rs.get_robot_params(self._state).get(param)

    async def retrieve_item(self, label: str):
        item = AxisCoordinate(rs.search_grid(self.get_storage()))
        await self.pick(item)

    async def place_item(self):
        # get next available position
        next = rs.find_first_available_cell(self.get_storage())
        if next is None:
            print("No available cell found.")
            return

        coordinate = AxisCoordinate(next["coord"])
        await self.place(coordinate)

    async def io_place(self, coord: AxisCoordinate):
        await self.pick(coord)

        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[1])
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[0]) 
        await self._md.move_to_position_no_wait(170)
        await self.pick_wait()

        await self._md.move_to_position(170) # todo make para
        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION"))
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE")+8)

        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[1])
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[0]) 
        await self.gantry_wait()
        await self._mg.retract()
        await self._ms.retract()

        await self._md.move_to_position_no_wait(0)
        await self._my.move_to_position_no_wait(coord.y)
        await self._mx.move_to_position_no_wait(coord.x)
        await self.pick_wait()

        await self.place(coord)

        await self._md.home()

    async def io_pick(self, coord):

        await self.pick(coord)
        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[1])
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_H")[0]) 
        await self._md.move_to_position_no_wait(170)
        await self.pick_wait()

        # await self._md.move_to_position(170)

        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PLACE")+8)
        await self.tool_wait()

        await self._my.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[1])
        await self._mx.move_to_position_no_wait(self.get_param("PICKUP_POSITION_L")[0]) 
        await self.gantry_wait()

        await self._mg.retract()
        await self._ms.retract()

        await self._md.move_to_position_no_wait(0)
        await self._my.move_to_position_no_wait(coord.y)
        await self._mx.move_to_position_no_wait(coord.x)
        await self.pick_wait()

        await self.place(coord)

        await self._md.home()

    async def pick(self, coordinate: AxisCoordinate) -> None:
        await self._gs.safe_open()
        await self._my.move_to_position_no_wait(coordinate.y)
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self.gantry_wait()
        #empty move
        await self._ms.move_to_position_no_wait(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(self.get_param("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

    async def place(self, coordinate: AxisCoordinate):
        await self._my.move_to_position_no_wait(coordinate.y)
        await self._mx.move_to_position_no_wait(coordinate.x)
        await self.gantry_wait()
        await self._ms.move_to_position(self.get_param("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position(self.get_param("GRIPPER_EXTENSION_PLACE"))
        await self._gs.close()

        await self._mg.retract()
        await self._ms.retract()

    # well plate
    # async def testtwo(self):
    #     await self._mr.move_to_position(0.5)
    #     print("andi")
    #     # time.sleep(2.5)
    #     while True:
    #         w = await self._scale.read_weight_data()
    #         if w > 50:
    #             await self.io_pick(AxisCoordinate(286.34, 384.79))
    #             return

    # big box
    async def testone(self, code):
        await self._mr.move_to_position(0.5)
        if code == "66666":
            # cl = AxisCoordinate(89.44, 42.548)
            cl = AxisCoordinate(914.44, 38.048)
        # await self._mr.move_to_position(0.5)
        # print("beblet")
        # # time.sleep(2.5)
        # while True:
        #     w = await self._scale.read_weight_data()
        #     if w > 50:
        #         await self.io_pick(AxisCoordinate(89.44, 42.548))
        #         return

            await self.io_place(cl)

        elif code == "11111":
            # cl = AxisCoordinate(89.44, 42.548)
            cl = AxisCoordinate(914.44, 38.048)
        # await self._mr.move_to_position(0.5)
        # print("beblet")
        # # time.sleep(2.5)
        # while True:
        #     w = await self._scale.read_weight_data()
        #     if w > 50:
        #         await self.io_pick(AxisCoordinate(89.44, 42.548))
        #         return

            await self.io_pick(cl)



    async def test(self) -> None:
              # bay = AxisCoordinate(round(bays[0]+2.5), round(bays[1])+2) #0.5, 3

        #+4 x +15 y
        await self._mr.move_to_position(0.5)

        s1 = [(910.44, 21.048), (745.44, 21.048), (580.44, 21.048), (415.44000000000005, 21.048), (250.44000000000005, 21.048), (85.44000000000005, 21.048)]
        s2 = [(912.344, 372.04), (807.344, 372.04), (702.344, 372.04), (597.344, 372.04), (492.34400000000005, 372.04), (387.34400000000005, 372.04), (282.34400000000005, 372.04)] 
        s3 = [(913.008, 575.44), (808.008, 575.44), (703.008, 575.44), (598.008, 575.44), (493.00800000000004, 575.44), (388.00800000000004, 575.44), (283.00800000000004, 575.44)] 
        s4 = [(921.272, 776.84), (831.272, 776.84), (741.272, 776.84), (651.272, 776.84), (561.272, 776.84), (471.27200000000005, 776.84), (381.27200000000005, 776.84), (291.27200000000005, 776.84)]
        s5 = [(921.68, 927.24), (831.68, 927.24), (741.68, 927.24), (651.68, 927.24), (561.68, 927.24), (471.67999999999995, 927.24), (381.67999999999995, 927.24), (291.67999999999995, 927.24)]
        
        sl1 = np.linspace(17, 21.5, 6, retstep=False)
        sl2 = np.linspace(10, 12.75, 7, retstep=False)
        sl3 = np.linspace(11.5, 14.25, 7, retstep=False)
        sl4 = np.linspace(10, 12.25, 8, retstep=False)
        sl5 = np.linspace(10, 13.5, 8, retstep=False)

        p1 = [(x + 4, y + dy) for (x, y), dy in zip(s1, sl1)]
        p2 = [(x + 4, y + dy) for (x, y), dy in zip(s2, sl2)]
        p3 = [(x + 4, y + dy) for (x, y), dy in zip(s3, sl3)]
        p4 = [(x + 4, y + dy) for (x, y), dy in zip(s4, sl4)]
        p5 = [(x + 4, y + dy) for (x, y), dy in zip(s5, sl5)]

        # await self._move(coordinate)
        
        # s1, 17 -> 21.5
        # s2, 10 - > 12.75
        # s3, 11.5 -> 14.25
        # s4, 10 -> 12.25
        # s5, 10 -> 13.5

        # 287.00800000000004, 589.44

        
        for i in range(0, 30):
            print(f"Cycle {i}")
            await self._move(AxisCoordinate(self.get_param("SCAN_POSITION")[0], 
                                            self.get_param("SCAN_POSITION")[1]))
            for tray in p1:
                # print(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.pick(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.place(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
            
            for tray in p2:
                # print(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.pick(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.place(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))

            for tray in p3:
                # print(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.pick(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.place(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
            
            for tray in p4:
                # print(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.pick(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.place(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))

            for tray in p5:
                # print(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.pick(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))
                await self.place(AxisCoordinate(round(tray[0], 4), round(tray[1], 4)))


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
            # cv2.imwrite("beb.png", frame)
            rect = self._cam.detect_white_squares(frame)
            # print(rect)
            if rect is not None:
                _, dy = self._cam.get_center(frame, rect)
                if dy > 0:
                    await self._my.jog(dy * self.get_param("PYMM")) # 30 (figure out bottom shelf)
                    frame = await self._cam.capture()
                    frame = self._cam.correct(frame)
                    # cv2.imwrite("beb2.png", frame)
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
    

    async def _move(self, coordinate: AxisCoordinate) -> None:
        await self._mx.move_to_position_no_wait(coordinate.x) 
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()

    async def move(self, coordinate: AxisCoordinate) -> None:
        await self._mr.move_to_position(0.5)
        await self.test()

    async def auto_cal(self):
        shelves = await self.get_shelves()
        anchors = await self.update_grid(shelves)
        # todo eventually make this auto generate the slope of each shelf by having two scan positions in x

    async def pick_wait(self):
        while True:
            mx_f = await self._mx.wait_for_move(timeout=10)
            my_f = await self._my.wait_for_move(timeout=10)
            md_f = await self._md.wait_for_move(timeout=10)

            if mx_f == MotorStatus.STOPPED and my_f == MotorStatus.STOPPED and md_f == MotorStatus.STOPPED:
                return


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
            case HomeCommand(barcode=code):
                await self.testone(code)
                return RobotCommandSuccessful()
            case MoveCommand(coordinate=coord):
                await self.move(coord)
                return RobotCommandSuccessful()
            case IdleCommand():
                await self.testtwo()
                return RobotCommandSuccessful()
            # case TestCommandOne():
            #     await self.testone()
            #     return RobotCommandSuccessful()
            # case TestCommandTwo():
            #     await self.testtwo()
            #     return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
