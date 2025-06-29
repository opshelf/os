from enum import StrEnum
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from pydantic import BaseModel, Field
from pymodbus.client.serial import AsyncModbusSerialClient
from perpetual_os1.common import CanAxisConfiguration
from perpetual_os1.door import Door, DoorConfiguration
from perpetual_os1.gantry import (
    GantryCommands,
    GantryConfiguration,
    Gantry,
    GantryCoordinate,
    GantryStatus,
)
from perpetual_os1.localizer import LocalizedResult, Localizer
from perpetual_os1.scale_buttons import ScaleButtons
from perpetual_os1.storage import Cell, StorageGrid, StorageModuleCellLocation, CellItem, StorageModuleSide
from perpetual_os1.tool import Tool, ToolCommands, ToolConfiguration, ToolStatus
from perpetual_can import CANBusHandler
from typing import List, Optional
from pydantic import BaseModel, Field
from typing import Union, Annotated, Literal
from perpetual_os1.gantry import GantryCoordinate
from perpetual_waveshare.digital import DigitalIOController
import os
import asyncio
from perpetual_os1.curtain import IntelligentGrating


class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"


class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"


class HomeCommand(BaseModel):
    command: Literal["home"] = "home"


class UnsafeMoveCommand(BaseModel):
    command: Literal["unsafe_move"] = "unsafe_move"
    coordinate: GantryCoordinate


class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    coordinate: GantryCoordinate


class IdleCommand(BaseModel):
    command: Literal["idle"] = "idle"


class GrabBoxCommand(BaseModel):
    command: Literal["grab_box"] = "grab_box"
    coordinate: GantryCoordinate
    width: float


class PlaceBoxCommand(BaseModel):
    command: Literal["place_box"] = "place_box"
    coordinate: GantryCoordinate
    width: float


class MoveToPickupLocationCommand(BaseModel):
    command: Literal["move_to_pickup_location"] = "move_to_pickup_location"


class DropOffItemCommand(BaseModel):
    command: Literal["drop_off_item"] = "drop_off_item"


class PickupItemCommand(BaseModel):
    command: Literal["pickup_item"] = "pickup_item"


class GrabCellCommand(BaseModel):
    command: Literal["grab_cell"] = "grab_cell"
    location: StorageModuleCellLocation


class PlaceCellCommand(BaseModel):
    command: Literal["place_cell"] = "place_cell"
    location: StorageModuleCellLocation


class StoreItemCommand(BaseModel):
    command: Literal["store_item"] = "store_item"
    location: StorageModuleCellLocation
    item: Optional[CellItem]


class RetrieveItemCommand(BaseModel):
    command: Literal["retrieve_item"] = "retrieve_item"
    location: StorageModuleCellLocation


class GantryCommand(BaseModel):
    command: Literal["gantry_command"] = "gantry_command"
    payload: GantryCommands


class ToolCommand(BaseModel):
    command: Literal["tool_command"] = "tool_command"
    payload: ToolCommands

class ReturnCellCommand(BaseModel):
    command: Literal["return_cell"] = "return_cell"

class SizeItemCommand(BaseModel):
    command: Literal["size_item"] = "size_item"


class ToggleScalePowerCommand(BaseModel):
    command: Literal["toggle_scale_power"] = "toggle_scale_power"

class ToggleScalePrintCommand(BaseModel):
    command: Literal["toggle_scale_print"] = "toggle_scale_print"

class ToggleScaleTareCommand(BaseModel):
    command: Literal["toggle_scale_tare"] = "toggle_scale_tare"

# Create a union of all Robot commands using a discriminator field
RobotCommands = Annotated[
    Union[
        StopCommand,
        InitializeCommand,
        HomeCommand,
        UnsafeMoveCommand,
        MoveCommand,
        IdleCommand,
        GrabBoxCommand,
        PlaceBoxCommand,
        MoveToPickupLocationCommand,
        DropOffItemCommand,
        PickupItemCommand,
        GrabCellCommand,
        PlaceCellCommand,
        StoreItemCommand,
        RetrieveItemCommand,
        GantryCommand,
        ToolCommand,
        ReturnCellCommand,
        SizeItemCommand,
        ToggleScalePowerCommand,
        ToggleScalePrintCommand,
        ToggleScaleTareCommand
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


class RobotConfiguration(BaseModel):
    tool: ToolConfiguration
    gantry: GantryConfiguration
    door: DoorConfiguration
    y_safe: float
    idle_position: GantryCoordinate
    place_offset: GantryCoordinate
    pickup_location: GantryCoordinate
    pickup_location_offset: GantryCoordinate
    curtain_axis_configuration: CanAxisConfiguration
    state_file: str = "state.json"


class RobotState(BaseModel):
    current_cell: Optional[StorageModuleCellLocation] = None


class RobotStatus(BaseModel):
    tool: ToolStatus
    gantry: GantryStatus
    storage: StorageGrid
    state: RobotState
    light_curtain: bool



class Robot:
    def __init__(
        self, bus: CANBusHandler, client: AsyncModbusSerialClient, configuration: RobotConfiguration
    ) -> None:
        self._configuration = configuration
        self._gantry = Gantry(bus, self._configuration.gantry)
        self._tool = Tool(bus, client, self._configuration.tool)
        self._io = DigitalIOController(client)
        self._scale_buttons = ScaleButtons(client)
        self._sizing_curtain = IntelligentGrating(client, address=0x40)
        self._safety_curtain = IntelligentGrating(client, address=0x41)
        self._door = Door(bus, self._configuration.door)
        self._curtain = TrinamicAxis(
            bus,
            configuration=self._configuration.curtain_axis_configuration.configuration,
            module_id=self._configuration.curtain_axis_configuration.can_id
        )
        self._localizer = Localizer(5.0, 25.0)
        self._bus = bus
        self._client = client
        self._storage = StorageGrid(left_module=[], right_module=[])
        self._state = RobotState()
        self.load_state()


    @property
    def curtain(self) -> TrinamicAxis:
        return self._curtain

    @property
    def state(self) -> RobotState:
        return self._state

    @property
    def configuration(self) -> RobotConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: RobotConfiguration) -> None:
        self._configuration = configuration
        await self.initialize()

    @property
    def door(self) -> Door:
        return self._door

    @property
    def gantry(self) -> Gantry:
        return self._gantry

    @property
    def tool(self) -> Tool:
        return self._tool

    def get_storage(self) -> StorageGrid:
        return self._storage

    def set_storage(self, value: StorageGrid) -> None:
        self._storage = value

    async def stop(self) -> None:
        await self._gantry.stop()
        await self._tool.stop()

    async def initialize(self) -> None:
        await self._gantry.set_configuration(self._configuration.gantry)
        await self._tool.set_configuration(self._configuration.tool)
        await self._gantry.initialize()
        await self._tool.initialize()
        await self._door.initialize()
        await self._curtain.initialize()

    async def home(self) -> None:
        await self.retract_stage()
        await self._tool.home()
        await self._gantry.home()
        await self._door.home()
        await self._curtain.home()

    async def unsafe_move(self, coordinate: GantryCoordinate) -> None:
        await self._gantry.move_coordinate(coordinate, True)

    async def move(self, coordinate: GantryCoordinate) -> None:
        await self._tool.retract_z()
        await self.retract_stage()
        current_position = await self._gantry.position
        move_path = self.calculate_move_path(current_position, coordinate)
        await self._gantry.move_path(move_path)

    def calculate_move_path(
        self, current_position: GantryCoordinate, target_position: GantryCoordinate
    ) -> List[GantryCoordinate]:
        path = []
        if (
            target_position.y < self._configuration.y_safe
            and current_position.y >= self._configuration.y_safe
        ):
            path.append(GantryCoordinate(x=target_position.x, y=current_position.y))
            path.append(target_position)
        elif (
            target_position.y < self._configuration.y_safe
            and current_position.y < self._configuration.y_safe
        ):
            path.append(GantryCoordinate(x=current_position.x, y=target_position.y))
            path.append(target_position)
        elif current_position.y < self._configuration.y_safe:
            path.append(GantryCoordinate(x=current_position.x, y=target_position.y))
            path.append(target_position)
        else:
            path.append(target_position)
        return path

    async def idle(self) -> None:
        await self.move(self._configuration.idle_position)

    async def grab_box(self, coordinate: GantryCoordinate) -> None:
        await self.move(coordinate)
        await self._tool.grab_box()

    async def place_box(self, coordinate: GantryCoordinate) -> None:
        await self.move(coordinate + self._configuration.place_offset)
        await self._tool.place_box()

    async def move_to_pickup_location(self) -> None:
        await self.tool.rotate_to_side(StorageModuleSide.RIGHT)
        await self.move(self._configuration.pickup_location)

    async def drop_off_item(self) -> None:
        await self.tool.rotate_to_side(StorageModuleSide.RIGHT)
        await self.move(self._configuration.pickup_location + self._configuration.place_offset)
        await self._door.open_door()
        await self.extend_stage()
        await self._tool.extend_pickup()
        await self.unsafe_move(
            self._configuration.pickup_location + self._configuration.pickup_location_offset
        )
        await self._tool.retract_z()
        await self.retract_stage()
        await self._door.close_door()

    async def pickup_item(self) -> None:
        await self.tool.rotate_to_side(StorageModuleSide.RIGHT)
        await self.move(
            self._configuration.pickup_location + self._configuration.pickup_location_offset
        )
        await self._door.open_door()
        await self.extend_stage()
        await self._tool.extend_pickup()
        await self.unsafe_move(self._configuration.pickup_location)
        await self._tool.retract_z()
        await self.retract_stage()
        await self._door.close_door()

    async def _grab_cell(self, coordinate: GantryCoordinate, cell: Cell) -> None:
        await self.move(coordinate)
        await self.extend_stage()
        await self._tool.grab_box()
        await self.retract_stage()

    async def _place_cell(self, coordinate: GantryCoordinate, cell: Cell) -> None:
        await self.move(coordinate + self._configuration.place_offset)
        await self.extend_stage()
        await self._tool.place_box_no_retract()
        await self.unsafe_move(coordinate)
        await self._tool.setup_box_drop()
        await self._tool.retract_z()
        await self.retract_stage()

    async def grab_cell(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_cell_coordinate(location)
        cell = self._storage.get_cell(location)
        await self._tool.rotate_to_side(location.side)
        self._state = RobotState(current_cell=location)
        await self._grab_cell(coordinate.to_gantry_coordinate(), cell)
        self.set_current_cell(location)

    async def place_cell(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_cell_coordinate(location)
        cell = self._storage.get_cell(location)
        await self._tool.rotate_to_side(location.side)
        self._state = RobotState(current_cell=None)
        await self._place_cell(coordinate.to_gantry_coordinate(), cell)
        self.clear_current_cell()

    async def return_cell(self) -> None:
        if self._state.current_cell is not None:
            await self.place_cell(self._state.current_cell)

    def save_state(self) -> None:
        with open(self._configuration.state_file, "w+") as f:
            f.write(self._state.model_dump_json())

    def load_state(self) -> None:
        if os.path.exists(self._configuration.state_file):
            with open(self._configuration.state_file) as f:
                self._state = RobotState.model_validate_json(f.read())
        else:
            self.save_state()

    def set_current_cell(self, cell: StorageModuleCellLocation) -> None:
        self._state.current_cell = cell
        self.save_state()

    def clear_current_cell(self) -> None:
        self._state.current_cell = None
        self.save_state()

    async def store_item(self, location: StorageModuleCellLocation) -> None:
        await self.grab_cell(location)
        await self.pickup_item()
        await self.place_cell(location)

    async def retrieve_item(self, location: StorageModuleCellLocation) -> None:
        await self.grab_cell(location)
        await self.drop_off_item()
        await self.place_cell(location)

    async def status(self) -> RobotStatus:
        gantry_status = await self._gantry.status()
        tool_status = await self._tool.status()
        blocked = False
        try:
            qty = await self._safety_curtain.read_blocked_quantity()
            blocked = qty > 0
        except Exception as e:
            print(e)
        
        return RobotStatus(
            gantry=gantry_status,
            tool=tool_status,
            storage=self._storage,
            state=self._state,
            light_curtain=blocked,
        )

    async def extend_stage(self) -> None:
        await self._io.set_channel_off(4)
        await asyncio.sleep(3)

    async def retract_stage(self) -> None:
        status = await self._io.read_output_channels()
        if status.ch4:
            await self._io.set_channel_on(4)
            await asyncio.sleep(3)


    async def get_broken_beams(self) -> CellSize:
        beam_sensors = await self._io.read_channels()
        if beam_sensors.ch0 == True or beam_sensors.ch5 == True:
            return CellSize.INVALID
        elif beam_sensors.ch1 == True or beam_sensors.ch4 == True:
            return CellSize.CELL_150
        elif beam_sensors.ch2 == True or beam_sensors.ch3 == True:
            return CellSize.CELL_100
        else:
            return CellSize.CELL_50

    def get_cell_size(self, cell: CellSize, max_bound: float) -> CellSize:
        if cell == CellSize.CELL_150:
            return CellSize.CELL_150
        elif max_bound >= 100.0:
            return CellSize.CELL_150
        elif max_bound < 100.0 and max_bound >= 50.0 and cell == CellSize.CELL_100:
            return CellSize.CELL_100
        elif max_bound < 100.0 and max_bound >= 50.0 and cell == CellSize.CELL_50:
            return CellSize.CELL_50
        elif max_bound < 50.0 and cell == CellSize.CELL_100:
            return CellSize.CELL_100
        elif max_bound < 50.0 and cell == CellSize.CELL_50:
            return CellSize.CELL_50
        else: 
            return CellSize.INVALID

    async def size_item(self) -> SizeResult:
        localizations: List[LocalizedResult] = []
        broken_beams = await self.get_broken_beams()
        if broken_beams == CellSize.INVALID:
            return SizeResult(
                result="item_outside_of_zone",
                size=None
            )
        try:
            await self._curtain.move_to_position(0)
            await self._curtain.init_brake_move()
            await self._curtain.move_to_position(220, override_brake=True)
            height = 20
            while True:
                beams = await self._sizing_curtain.read_all_light_status(31)
                height = await self._curtain.get_position() + 20
                result = self._localizer.localize(beams)
                if result:
                    localizations.append(result)
                    pass
                else:
                    await self._curtain.stop()
                    break
                position_reached = await self._curtain.get_position_reached()
                if position_reached:
                    break
            await self._curtain.move_to_position(0)
            max_object_size = 0.0
            min_bound = 300
            max_bound = 0

            for localization in localizations:
                if localization.object_size > max_object_size:
                    max_object_size = localization.object_size
                if localization.first_break_location < min_bound:
                    min_bound = localization.first_break_location
                if localization.last_break_location > max_bound:
                    max_bound = localization.last_break_location
            if max_object_size > 150.0:
                return SizeResult(
                    result="item_outside_of_zone",
                    size=None
                )
            else:
                return SizeResult(
                    result="detected",
                    size=ItemSizeLocation(
                        object_size=max_object_size,
                        min_bound=min_bound,
                        max_bound=max_bound,
                        height=height,
                        cell_size=self.get_cell_size(broken_beams, max_bound)
                    )
                )

        finally:
            await self._curtain.end_brake_move()

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
            case UnsafeMoveCommand(coordinate=coord):
                await self.unsafe_move(coord)
                return RobotCommandSuccessful()
            case MoveCommand(coordinate=coord):
                await self.move(coord)
                return RobotCommandSuccessful()
            case IdleCommand():
                await self.idle()
                return RobotCommandSuccessful()
            case GrabBoxCommand(coordinate=coord, width=width):
                await self.grab_box(coord)
                return RobotCommandSuccessful()
            case PlaceBoxCommand(coordinate=coord, width=width):
                await self.place_box(coord)
                return RobotCommandSuccessful()
            case MoveToPickupLocationCommand():
                await self.move_to_pickup_location()
                return RobotCommandSuccessful()
            case DropOffItemCommand():
                await self.drop_off_item()
                return RobotCommandSuccessful()
            case PickupItemCommand():
                await self.pickup_item()
                return RobotCommandSuccessful()
            case GrabCellCommand(location=loc):
                await self.grab_cell(loc)
                return RobotCommandSuccessful()
            case PlaceCellCommand(location=loc):
                await self.place_cell(loc)
                return RobotCommandSuccessful()
            case StoreItemCommand(location=loc, item=item):
                await self.store_item(loc)
                self._storage.store_item(loc, item)
                return RobotItemStoredResponse(location=loc)
            case RetrieveItemCommand(location=loc):
                await self.retrieve_item(loc)
                self._storage.clear_item(loc)
                self._storage.save()
                return RobotCommandSuccessful()
            case GantryCommand(payload=payload):
                await self._gantry.handle_command(payload)
                return RobotCommandSuccessful()
            case ToolCommand(payload=payload):
                await self._tool.handle_command(payload)
                return RobotCommandSuccessful()
            case ReturnCellCommand():
                await self.return_cell()
                return RobotCommandSuccessful()
            case SizeItemCommand():
                ret = await self.size_item()
                return ret
            case ToggleScaleTareCommand():
                await self._scale_buttons.tare()
                return RobotCommandSuccessful()
            case ToggleScalePrintCommand():
                await self._scale_buttons.print()
                return RobotCommandSuccessful()
            case ToggleScalePowerCommand():
                await self._scale_buttons.power()
                return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
