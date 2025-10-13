import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from perpetual_os1.iobox import IOBox, IOBoxStatus, SizeResult, ItemSizeLocation
from perpetual_os1.tool import Tool, ToolStatus, ToolMotionStatus, ToolItemStatus

from pathlib import Path
from enum import Enum
from typing import Dict, Optional, Tuple, Union, Literal, Annotated
from pydantic import BaseModel, Field

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"

class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"

class HomeCommand(BaseModel):
    command: Literal["home"] = "home"

class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    coordinate: c.AxisCoordinate

class IdleCommand(BaseModel):
    command: Literal["idle"] = "idle"

class StoreCommand(BaseModel):
    command: Literal["store"] = "store"
    item: str

class RetrieveCommand(BaseModel):
    command: Literal["retrieve"] = "retrieve"
    item: str

RobotCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        MoveCommand,
        IdleCommand,
        StoreCommand,
        RetrieveCommand,
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

class RobotStatus(str, Enum):
    RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, SizeResult]

class RobotState(BaseModel):
    io: IOBoxStatus
    tool: ToolStatus
    # current_cell: Optional[StorageModuleCellLocation] = None

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, SizeResult]

# class WorkerStatus(BaseModel):
#     robot_status: Optional[RobotStatus]
#     running: bool
#     current_command: Optional[RobotCommands]

class Robot:
    def __init__(self) -> None:
        # TODO add port retries, config, hardening
        self._motorbus = Controller(port="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B001XSSJ-if00-port0", baudrate=115200) # change to config
        self._scalebus = Controller(port="/dev/serial/by-id/usb-1a86_USB_Single_Serial_594D007725-if00", baudrate=115200) # change to config

        # Load State
        self._state = c.load_config_file(CONFIG_PATH)
        tool_params = self._state.get("robot.tool", {})
        io_params = self._state.get("robot.io", {})

        # Robot Components
        self._tool = Tool(motorcontroller=self._motorbus, params=tool_params)
        self._io = IOBox(motorcontroller=self._motorbus, params=io_params)
        # self._grid: Dict[str, cval.GridCell] = {} # storage grid

        # Status
        self._cur_item: str = None # some kind of unique identifier
        self._status: RobotStatus = None
        self._state: RobotState = RobotState(io=IOBoxStatus.IDLE, tool=ToolStatus(motion=ToolMotionStatus.IDLE, item=ToolItemStatus.EMPTY))

    async def configure(self):
        await self._motorbus.connect()
        if not self._motorbus.client.connected:
            print("No Motor bus")
            return
        # await self._scalebus.connect()
        # if not self._scalebus.client.connected:
        #     print("No scale bus")
        #     return
        
        await self._tool.configure()
        # await self._io.configure()

    def get_param(self, param: str):
        return self._state.get("robot",{}).get(param, None)

    async def io_place(self, coord: c.AxisCoordinate):
        await self._tool.pick(coord)
        await self._tool.move(c.AxisCoordinate(self.get_param("PICKUP_POSITION_L")[0], 
                                               self.get_param("PICKUP_POSITION_L")[1])) # move to safe height
        await self._io.open_door()
        await self._tool.extend()
        await self._tool.move(c.AxisCoordinate(self.get_param("PICKUP_POSITION_H")[0], 
                                               self.get_param("PICKUP_POSITION_H")[1]))
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

    async def io_pick(self, coord):
        await self._tool.pick(coord)
        await self._tool.move(c.AxisCoordinate(self.get_param("PICKUP_POSITION_H")[0], 
                                               self.get_param("PICKUP_POSITION_H")[1])) # move to safe height
        await self._io.open_door()
        await self._tool.extend()
        await self._tool.move(c.AxisCoordinate(self.get_param("PICKUP_POSITION_L")[0], 
                                               self.get_param("PICKUP_POSITION_L")[1])) # move to safe height
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(c.AxisCoordinate(coord))
        await self._tool.place(coord)

        await self._io._md.home()
        await self._tool.clear_alarms()

    #############################

    async def idle(self) -> None:
        await self._tool.move(self.get_param("IDLE_POSITION"))

    async def shutdown(self) -> None:
        await self.stop()
        if self._motorbus:
            await self._motorbus.close()
        # if self._scalebus:
        #     await self._scalebus.close()

    async def stop(self) -> None:
        await self._tool.stop()
        await self._io.stop()
    
    async def handle_command(self, command: RobotCommands):
        match command:
            case StopCommand():
                await self.stop()
                return RobotCommandSuccessful()
            case InitializeCommand():
                await self.configure()
                return RobotCommandSuccessful()
            case HomeCommand():
                await self._tool.home()
                return RobotCommandSuccessful()
            case MoveCommand(coordinate=coord):
                await self._tool.move(coord)
                # await self._tool.place(coord)
                return RobotCommandSuccessful()
            case IdleCommand():
                await self.idle()
                return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
