from typing import Dict, Optional, Tuple, Union, Literal, Annotated
from perpetual_os1.common import AxisCoordinate, ToolCoordinate
from hardware.ModbusController import SharedModbusController as Controller
from hardware.MotorBase import Motor, Servo, MotorStatus
from hardware.camera import Camera
from hardware.scale import Scale
from hardware.curtain import Curtain
from pydantic import BaseModel, Field
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
        # self._md = Motor(
        #     self._bus, 
        #     rs.get_motor_params(self._state, "y_axis"), 
        # )

        self._gs = Servo(self._bus)
        self._cam = Camera(exp_area = rs.get_robot_params(self._state).get("EXPECTED_AREA"),
                           exp_asp = rs.get_robot_params(self._state).get("EXPECTED_ASPECT"))
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
        # # await self._md.configure()
        await self._gs.configure()

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
        await self._mx.move_to_position_no_wait(rs.get_robot_params(self._state).get("pickup_location").x)
        await self._my.move_to_position_no_wait(rs.get_robot_params(self._state).get("pickup_location").y)
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

    async def move(self, coordinate: AxisCoordinate) -> None:
        await self.update_grid()

    async def _move(self, coordinate: AxisCoordinate) -> None:
        await self._mx.move_config()
        await self._my.move_config()
        await self._mx.move_to_position_no_wait(coordinate.x) 
        await self._my.move_to_position_no_wait(coordinate.y)
        await self.gantry_wait()

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
    async def update_grid(self):
        await self.home()
        await self._mr.move_to_position(0.5)
        await self._move(AxisCoordinate(rs.get_robot_params(self._state).get("PICKUP_POSITION")[0], 
                                       rs.get_robot_params(self._state).get("PICKUP_POSITION")[1]))
        while True: 
            await self._mx.move_to_position(rs.get_robot_params(self._state).get("PICKUP_POSITION")[0])
            await asyncio.sleep(0.5)         
            pos_y = await self.auto_cal_y()
            if pos_y:
                while True:
                    pos_x = await self.auto_cal_x()
                    if pos_x:
                        rs.upsert_grid_cell(
                            state = self._state,
                            cell_id = None,
                            coord = AxisCoordinate(pos_x, pos_y),
                            size = [0,0]
                        )
                        print(pos_x, pos_y)
                        await asyncio.sleep(0.5)
                        await self._mx.jog(rs.get_robot_params(self._state).get("JOG_X"))
                        end_x = await self._mx.get_position()
                        end_x = self._mx._params["mpr"]*end_x/(rs.get_robot_params(self._state).get("PPR"))
                        if end_x > rs.get_motor_params(self._state, "x_axis").get("lim"):
                            break

                    await asyncio.sleep(0.5)
                await self._my.jog(rs.get_robot_params(self._state).get("JOG_Y"))

            await asyncio.sleep(0.5)

            end_y = await self._my.get_position()
            end_y = self._my._params["mpr"]*end_y/(rs.get_robot_params(self._state).get("PPR"))
            if end_y > rs.get_motor_params(self._state, "y_axis").get("lim"):
                break

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
        await self._my.home()

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
    
    async def centering(self, frame):
        # frame = cv2.rotate(frame, cv2.ROTATE_180)
        height, width = frame.shape[:2]
        third = width // 3
        third_h = height // 3

        frame_middle = frame[:, third:2 * third]
        frame_middle = frame_middle[2 * third_h:, :]
        # frame_middle = frame

        w_e = rs.get_robot_params(self._state).get("W_EXPECTED") 
        h_e = rs.get_robot_params(self._state).get("H_EXPECTED")


        bbox_crop = self._cam.get_fiducial(frame_middle,
                                           w_e * h_e,
                                           w_e / h_e,
                                           rs.get_robot_params(self._state).get("TOLERANCE_AREA"),
                                           rs.get_robot_params(self._state).get("TOLERANCE_ASPECT"))


        # cv2.imwrite("beb.png",frame_middle)

        if bbox_crop is None:
            print("No white rectangular fiducial detected.")
            return None, None
        else:
            x, y, w, h = bbox_crop
            # x = x_crop + third  # Shift X to full-frame coordinates
            _ , obj_center, frame_center = self._cam.get_center(frame_middle, bbox_crop)
            # print(f"Centered score (0 = perfect): {score:.3f}")
            # print(f"Object center: {obj_center}")
            # print(f"Frame center: {frame_center}")

            # Draw bounding box and centers
            x, y, w, h = bbox_crop
            cv2.rectangle(frame_middle, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.circle(frame_middle, (int(obj_center[0]), int(obj_center[1])), 5, (255, 0, 0), -1)
            cv2.circle(frame_middle, (int(frame_center[0]), int(frame_center[1])), 5, (0, 0, 255), -1)
            offset_mm_x = (obj_center[0]-frame_center[0])*rs.get_robot_params(self._state).get("PXMM")
            offset_mm_y = (obj_center[1]-frame_center[1])*rs.get_robot_params(self._state).get("PYMM")

            cv2.imwrite("beb.png",frame_middle)
            
            return offset_mm_x, offset_mm_y

    async def auto_cal_x(self):
        frame = self._cam.capture_sync()
        offset, _ = await self.centering(frame)
        if offset is None:
            await self._mx.jog(rs.get_robot_params(self._state).get("JOG_X"))
            return offset
        else:
            await self._mx.jog(offset) # camera is upside down
            pos = await self._mx.get_position()
            pos = ((self._mx._params["mpr"]*pos)/rs.get_robot_params(self._state).get("PPR")) #in mm
            return pos
        
    async def auto_cal_y(self):
        frame = self._cam.capture_sync()
        _ , offset = await self.centering(frame)
        if offset is None:
            await self._my.jog(rs.get_robot_params(self._state).get("JOG_Y"))
            return offset
        else:
            await self._my.jog(offset) # camera is upside down?
            pos = await self._my.get_position()
            pos = ((self._my._params["mpr"]*pos)/rs.get_robot_params(self._state).get("PPR")) #in mm
            return pos


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
