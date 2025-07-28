from enum import Enum
from pydantic import BaseModel, Field
from perpetual_os1.configuration import motor_x, motor_stage, motor_gripper, motor_rotate
from perpetual_os1.common import AxisCoordinate, ToolCoordinate
from hardware.ModbusController import SharedModbusController as Controller
from hardware.MotorBase import Motor, Servo
# from storage import Cell, StorageGrid, StorageModuleCellLocation, CellItem, StorageModuleSide
from typing import Optional
from typing import Union, Annotated, Literal

import asyncio
SLIDE_EXTENSION = 45
GRIPPER_EXTENSION = 170
REVERSE = 180

BAY1 = 19
BAY2 = 174
BAY3 = 329
BAY4 = 484

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

# TODO: MORE DICTS

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
        # todo motor list

    async def move(self, coordinate: AxisCoordinate) -> None:
        await self.home()
        await self._mr.move_to_position(REVERSE)
        await self.pick(AxisCoordinate(BAY1, 0))
        await self.place(AxisCoordinate(BAY2, 0))
        await self.pick(AxisCoordinate(BAY2, 0))
        await self.place(AxisCoordinate(BAY3, 0))
        await self.pick(AxisCoordinate(BAY3, 0))
        await self.place(AxisCoordinate(BAY4, 0))
        await self.pick(AxisCoordinate(BAY4, 0))
        await self.place(AxisCoordinate(BAY1, 0))

    async def pick(self, coordinate: AxisCoordinate) -> None:
        await self._gs.safe_open()
        await self._mx.move_to_position(coordinate.x)
        await self._ms.move_to_position(SLIDE_EXTENSION) # 50 max
        await self._mg.move_to_position(GRIPPER_EXTENSION)
        await self._gs.open()

        await self._mg.retract()
        await self._ms.retract()
        

    async def place(self, coordinate: AxisCoordinate):
        await self._mx.move_to_position(coordinate.x)
        await self._ms.move_to_position(SLIDE_EXTENSION) # 50 max
        await self._mg.move_to_position(GRIPPER_EXTENSION)
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

    # def get_storage(self) -> StorageGrid:
    #     return self._storage

    # def set_storage(self, value: StorageGrid) -> None:
    #     self._storage = value

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
                return RobotCommandSuccessful()
            # case MoveToolCommand(coordinate=coord):
            #     await self.tool_move(coord)
            #     return RobotCommandSuccessful()
            # case IdleCommand():
            #     await self.idle()
            #     return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
