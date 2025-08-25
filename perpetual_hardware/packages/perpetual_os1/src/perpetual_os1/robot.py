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
# Detection utilities
# ---------------------------
@dataclass
class Detection:
    bbox: Tuple[int, int, int, int]  # x, y, w, h
    center: Tuple[int, int]
    score: float

def iou(a, b) -> float:
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    x1 = max(ax, bx); y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw); y2 = min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0

def nms(dets: List[Detection], thr: float) -> List[Detection]:
    dets = sorted(dets, key=lambda d: d.score, reverse=True)
    kept = []
    for d in dets:
        if all(iou(d.bbox, k.bbox) < thr for k in kept):
            kept.append(d)
    return kept

def detect_square_fiducials(frame: np.ndarray) -> List[Detection]:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    # Adaptive threshold works well for uneven lighting; invert if your fiducial is white on black
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY, 15, -5)
    # Clean small holes/specks
    kernel = np.ones((3, 3), np.uint8)
    clean = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel)

    contours, _ = cv2.findContours(clean, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    dets: List[Detection] = []
    H, W = gray.shape[:2]

    for c in contours:
        area = cv2.contourArea(c)
        if area < 100:
            continue
        epsilon = 0.02 * cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, epsilon, True)
        if len(approx) != 4:
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if w == 0 or h == 0:
            continue
        aspect = w / float(h)
        if abs(aspect - 1.0) > 0.5:
            continue

        cx = x + w // 2
        cy = y + h // 2
        # score: larger & more centered gets higher score
        center_dist = np.hypot(cx - W // 2, cy - H // 2)
        score = area - 0.5 * center_dist
        dets.append(Detection((x, y, w, h), (cx, cy), float(score)))

    # Frame-level de-bounce
    dets = nms(dets, 0.3)
    return dets

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

# ---------------------------
# Centering routine
# ---------------------------

# ---------------------------
# Logger
# ---------------------------
# class CsvLogger:
#     def __init__(self, path: str):
#         self.path = path
#         with open(self.path, "w", newline="") as f:
#             w = csv.writer(f)
#             w.writerow(["ts", "x_world", "y_world", "pixel_cx", "pixel_cy"])

#     def log(self, xw: float, yw: float, cx: int, cy: int):
#         with open(self.path, "a", newline="") as f:
#             w = csv.writer(f)
#             w.writerow([time.time(), xw, yw, cx, cy])

# ---------------------------
# Raster scan (serpentine)
# ---------------------------

    # Final home (optional)
    # await motors.home_y()

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

        # await self.home()

        # await self._scale.configure()
        # await self._curtain.connect()

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
        await self._mx.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_LOCATION_L")[0])
        await self._my.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_LOCATION_L")[1])
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
        await self._ms.move_to_position_no_wait(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PICK"))
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
        await self._ms.move_to_position(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PLACE"))
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
        await self._ms.move_to_position_no_wait(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_L")[0]) 
        await self._my.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_L")[1])
        await self.gantry_wait()

        await self._md.move_to_position(170)

        await self._ms.move_to_position(rs.get_robot_params(self._state).get("SLIDE_EXTENSION"))
        await self._mg.move_to_position(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PLACE")+5)

        await self._mx.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_H")[0]) 
        await self._my.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_H")[1])
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
        await self._ms.move_to_position_no_wait(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PLACE"))
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
        await self._ms.move_to_position_no_wait(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PICK"))
        await self.tool_wait()

        await self._gs.open()
        await self._mg.retract()
        await self._ms.retract()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_H")[0]) 
        await self._my.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_H")[1])
        await self.gantry_wait()

        await self._md.move_to_position(170)

        await self._ms.move_config()
        await self._mg.move_config()
        await self._ms.move_to_position_no_wait(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PLACE")+5)
        await self.tool_wait()

        # await self._mx.move_config()
        # await self._my.move_config()
        await self._mx.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_L")[0]) 
        await self._my.move_to_position_no_wait(rs.get_robot_params(self._state).get("PICKUP_POSITION_L")[1])
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
        await self._ms.move_to_position_no_wait(rs.get_robot_params(self._state).get("SLIDE_EXTENSION")) # 50 max
        await self._mg.move_to_position_no_wait(rs.get_robot_params(self._state).get("GRIPPER_EXTENSION_PLACE"))
        await self.tool_wait()

        await self._gs.close()
        await self._mg.retract()
        await self._ms.retract()

    async def center_fiducial(self, frame) -> Optional[Tuple[int, int]]:
        """Try to center the best fiducial; returns centered pixel (cx, cy) or None."""
        H, W = frame.shape[:2]
        dets = detect_square_fiducials(frame)
        if not dets:
            return None

        # Pick the highest score
        det = max(dets, key=lambda d: d.score)
        cx, cy = det.center
        dx_px = (W // 2) - cx
        dy_px = (H // 2) - cy

        if abs(dx_px) <= 5 and abs(dy_px) <= 5:
            return (cx, cy)

        # Convert pixel error to motor deltas (in mm or steps)
        # Sign convention: if fiducial is to the LEFT (cx < center), you likely need to move +X to bring it right.
        # Adjust sign if your camera axes are flipped.
        dx = dx_px * 0.06
        dy = dy_px * 0.06

        # await self.move_rel(dx, dy)
        await self._mx.jog(dx)
        await self._my.jog(dy)

        return dx, dy

        # ok = await motors.wait_idle(cfg.move_timeout_s)
        # if not ok:
        #     return None

        # Re-check once more and finish
        # frame2 = self._cam.read()
        # H2, W2 = frame2.shape[:2]
        # dets2 = detect_square_fiducials(frame2)
        # if not dets2:
        #     return None
        # det2 = max(dets2, key=lambda d: d.score)
        # cx2, cy2 = det2.center
        # if abs((W2 // 2) - cx2) <= 5 and abs((H2 // 2) - cy2) <= 5:
        #     return (cx2, cy2)
        # return None

    async def update_grid(self):
        reg = FiducialRegistry()

        cap = cv2.VideoCapture(self._cam.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError("bad cam")

        # Start known position
        # await motors.move_abs(cfg.x_min, cfg.y_min)
        # await motors.wait_idle(cfg.move_timeout_s)
        await self.home()
        await self._mr.move_to_position(0.5)
        await self._move(AxisCoordinate(rs.get_robot_params(self._state).get("SCAN_POSITION")[0], 
                                       rs.get_robot_params(self._state).get("SCAN_POSITION")[1]))

        end_x = 0
        end_y = 0

        time.sleep(5)

        for i in range(0,7):
            for _ in range(5):  # throw away old frames
                cap.read()
            ok, frame = cap.read()
            if not ok:
                return

            if frame is None:
                print("bad frame")
                return
            
            height, width = frame.shape[:2]
            third = width // 3
            # third_h = height // 3

            frame = frame[:, third:2 * third]
            # frame = frame[2 * third_h:, :]

            frame = cv2.resize(frame, None, fx=1.1, fy=1, interpolation=cv2.INTER_LINEAR)
            # frame = frame[2 * third_h:, :]
            # TODO add NoneType handling
            # cv2.imwrite("beb.jpg", frame)
            rect = self._cam.detect_white_squares(frame)
            # print(rect)
            if rect is not None:
                dx, _ = self._cam.get_center(frame, rect)
                # only track increasing boxes
                if dx is not None:
                    await self._mx.jog(dx * 0.06)
                    # await self._my.jog(dy* 0.06)
        
                    # Get world pos AFTER centering
                    xw = round(await self._mx.get_position(), 4)
                    yw = round(await self._my.get_position(), 4)

                    # Global de-dup in world coordinates
                    if reg.seen(xw, yw, 20):
                        # already captured close to here
                        return

                    reg.add(xw, yw)
                    print(f"xw: {xw}")

            await self._mx.jog(90)
        cap.release()
        

        # ok, frame = cap.read()
        # if not ok:
        #     return

        # if frame is None:
        #     print("bad frame")
        #     return

        # # height, width = frame.shape[:2]
        # # third = width // 3
        # # third_h = height // 3

        # # frame = frame[:, third:2 * third]
        # # frame = frame[2 * third_h:, :]
        # frame = cv2.resize(frame, None, fx=1.1, fy=1, interpolation=cv2.INTER_LINEAR)

        # # TODO add NoneType handling
        # rect = self._cam.detect_white_squares(frame)
        # if rect is not None:
        #     dx, _ = self._cam.get_center(frame, rect)
        
        #     if dx is not None:
        #         await self._mx.jog(dx * 0.06)
        #         # await self._my.jog(dy* 0.06)
    
        #         # Get world pos AFTER centering
        #         xw = await self._mx.get_position()
        #         yw = await self._my.get_position()

        #         # Global de-dup in world coordinates
        #         if reg.seen(xw, yw, 20):
        #             # already captured close to here
        #             return

        #         reg.add(xw, yw)
        #         print(f"xw: {xw}")

        # while True:
        #     while True:
        #         ok, frame = cap.read()
        #         if not ok:
        #             break

        #         if frame is None:
        #             print("bad frame")
        #             break

        #         frame = cv2.resize(frame, None, fx=1.1, fy=1, interpolation=cv2.INTER_LINEAR)
        #         height, width = frame.shape[:2]
        #         third = width // 3
        #         third_h = height // 3

        #         frame = frame[:, third:2 * third]
        #         frame = frame[2 * third_h:, :]
        #         # TODO add NoneType handling
        #         rect = self._cam.detect_white_squares(frame)
        #         if rect is not None:
        #             dx, dy = self._cam.get_center(frame, rect)
                
        #             if dx is not None and dy is not None:
        #                 await self._mx.jog(dx* 0.06)
        #                 # await self._my.jog(dy* 0.06)
            
        #                 # Get world pos AFTER centering
        #                 xw = await self._mx.get_position()
        #                 yw = await self._my.get_position()

        #                 # Global de-dup in world coordinates
        #                 if reg.seen(xw, yw, 20):
        #                     # already captured close to here
        #                     break

        #                 reg.add(xw, yw)
        #                 print(f"xw: {xw}")

        #         await self._mx.jog(rs.get_robot_params(self._state).get("JOG_X"))
        #         end_x = await self._mx.get_position()
        #         if end_x + rs.get_robot_params(self._state).get("JOG_X") > rs.get_motor_params(self._state, "x_axis").get("lim"):
        #             print("xlim")
        #             break

        #     await self._mx.move_to_position(rs.get_robot_params(self._state).get("SCAN_POSITION")[0])
        #     end_y = await self._my.get_position()
        #     if end_y + rs.get_robot_params(self._state).get("JOG_Y") > rs.get_motor_params(self._state, "y_axis").get("lim")*0.9:
        #         print("ylim")
        #         break
        #     await self._my.jog(rs.get_robot_params(self._state).get("JOG_Y"))


    async def _move(self, coordinate: AxisCoordinate) -> None:
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x) 
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()

    async def move(self, coordinate: AxisCoordinate) -> None:
        await self.update_grid()
        # await self._mx.move_config()
        # await self._my.move_config()
        # await self._mx.move_to_position_no_wait(coordinate.x) 
        # await self._my.move_to_position_no_wait(coordinate.y)
        # await self.gantry_wait()
        # time.sleep(5)
        # frame = await self._cam.capture("beb.png")
        # jog = self._cam.get_shelf_fiducial(frame)*rs.get_robot_params(self._state).get("PYMM")
        # await self._my.jog(jog+187)
        # time.sleep(5)
        # frame = await self._cam.capture("bebcent.png")
        # fdc = self._cam.get_shelf_fiducial(frame)
        # jog = fdc*rs.get_robot_params(self._state).get("PYMM")
        # print(jog)

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

    # todo, 2D and update grid
    async def _update_grid(self):
        await self.home()
        await self._mr.move_to_position(0.5)
        await self._move(AxisCoordinate(rs.get_robot_params(self._state).get("SCAN_POSITION")[0], 
                                       rs.get_robot_params(self._state).get("SCAN_POSITION")[1]))
        
        time.sleep(1)

        bays = []
        
        while True:
            jog_count = 0
            end_y = await self._my.get_position()
            if end_y + rs.get_robot_params(self._state).get("JOG_Y") > rs.get_motor_params(self._state, "y_axis").get("lim")*0.9:
                print("ylim")
                break
            while True:
                time.sleep(0.5)
                pos = await self.auto_cal()
                if pos is not None:
                    pos_x, pos_y = pos
                    pos_x = round(pos_x, 4)
                    pos_y = round(pos_y, 4)
                    # also check if the grid cell already exists
                    rs.upsert_grid_cell(
                        state = self._state,
                        cell_id = None,
                        coord = AxisCoordinate(pos_x, pos_y),
                        size = [0,0]
                    )
                    await self._mx.jog(rs.get_robot_params(self._state).get("JOG_X"))
                    time.sleep(0.5)
                    end_x = await self._mx.get_position()
                    if end_x + rs.get_robot_params(self._state).get("JOG_X") > rs.get_motor_params(self._state, "x_axis").get("lim"):
                        print("xlim")
                        break
                else:
                    jog_count += 1
                    if jog_count > 7:
                        # print("jog")
                        break

            await self._mx.move_to_position(rs.get_robot_params(self._state).get("SCAN_POSITION")[0])
            await self._my.jog(rs.get_robot_params(self._state).get("JOG_Y"))

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
        await self.move(rs.get_robot_params(self._state).get("IDLE_POSITION")) # basically anywhere along the bottom of the gantry

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
