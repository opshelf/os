from enum import StrEnum
from pydantic import BaseModel, Field
from pymodbus.client.serial import AsyncModbusSerialClient
from common import AxisCoordinate, ToolCoordinate
from hardware.ModbusController import SharedModbusController as Controller
from hardware.MotorBase import Motor, motor_x, motor_stage, motor_gripper, motor_rotate
from perpetual_os1.src.door import Door, DoorConfiguration
from perpetual_os1.src.scale_buttons import ScaleButtons
from perpetual_os1.src.storage import Cell, StorageGrid, StorageModuleCellLocation, CellItem, StorageModuleSide
from typing import List, Optional
from typing import Union, Annotated, Literal
import os
import asyncio


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
    command: Literal["move"] = "move"
    coordinate: ToolCoordinate


class IdleCommand(BaseModel):
    command: Literal["idle"] = "idle"

RobotCommands = Annotated[
    Union[
        StopCommand,
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

class CellSize(StrEnum):
    CELL_50 = "CELL_50"
    CELL_100 = "CELL_100"
    CELL_150 = "CELL_150"
    INVALID = "INVALID"

class ItemSizeLocation(BaseModel):
    object_size: float
    min_bound: float
    max_bound: float
    height: float
    cell_size: CellSize 

class SizeResult(BaseModel):
    result: Union[Literal["detected"], Literal["no_item_detected"], Literal["item_outside_of_zone"]]
    size: Optional[ItemSizeLocation]

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, RobotItemStoredResponse, SizeResult]


class WorkerStatus(BaseModel):
    robot_status: Optional[str]
    running: bool
    current_command: Optional[RobotCommands]

class RobotState(BaseModel):
    current_cell: Optional[StorageModuleCellLocation] = None


# class RobotStatus(BaseModel):
#     tool: ToolStatus
#     gantry: GantryStatus
#     storage: StorageGrid
#     state: RobotState
#     light_curtain: bool

class Robot:
    def __init__(self, port: str) -> None:
        self._bus = Controller(port=port)

        self._mx = Motor(self.bus, motor_x)
        self._mg = Motor(self.bus, motor_gripper)
        self._mr = Motor(self.bus, motor_rotate)
        self._ms = Motor(self.bus, motor_stage)

        self._mx.configure()
        self._mg.configure()
        self._mr.configure()
        self._ms.configure()

    # todo track coordinates somewhere
    def move(self, coordinate: AxisCoordinate) -> None:
        self._mx.move_to_position(coordinate.x)

    def tool_move(self, coordinate: ToolCoordinate) -> None:
        self._ms.move_to_position(coordinate.s)
        self._mg.move_to_position(coordinate.g)

    def home(self) -> None:
        self._mx.home()
        self._mg.home()
        self._mr.home()
        self._ms.home()

    def stop(self) -> None:
        self._mx.stop()
        self._mg.stop()
        self._mr.stop()
        self._ms.stop()

    def get_pos(self) -> tuple:
        pass

    def shutdown(self) -> None:
        self._bus.close()

    @property
    def door(self) -> Door:
        return self._door

    def get_storage(self) -> StorageGrid:
        return self._storage

    def set_storage(self, value: StorageGrid) -> None:
        self._storage = value

    async def idle(self) -> None:
        await self.move(self._configuration.idle_position)

    async def shutdown(self) -> None:
        self._bus.shutdown()
        self._client.close()

    async def handle_command(self, command: RobotCommands) -> RobotResponse:
        match command:
            case StopCommand():
                await self.stop()
                return RobotCommandSuccessful()
            case InitializeCommand():
                await self.initialize()
                return RobotCommandSuccessful()
            case HomeCommand():
                await self.home()
                return RobotCommandSuccessful()
            case MoveCommand(coordinate=coord):
                await self.move(coord)
                return RobotCommandSuccessful()
            case IdleCommand():
                await self.idle()
                return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
