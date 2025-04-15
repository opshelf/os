from pydantic import BaseModel, Field
from perpetual_os1.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_os1.storage import Cell, StorageGrid, StorageModuleCellLocation
from perpetual_os1.tool import Tool, ToolConfiguration
from perpetual_can import CANBusHandler
from typing import List
from pydantic import BaseModel, Field
from typing import Union, Annotated, Literal
from perpetual_os1.gantry import GantryCoordinate

class StopCommand(BaseModel):
    command: Literal["stop"] = Field(default="stop")

class InitializeCommand(BaseModel):
    command: Literal["initialize"] = Field(default="initialize")

class HomeCommand(BaseModel):
    command: Literal["home"] = Field(default="home")

class UnsafeMoveCommand(BaseModel):
    command: Literal["unsafe_move"] = Field(default="unsafe_move")
    coordinate: GantryCoordinate

class MoveCommand(BaseModel):
    command: Literal["move"] = Field(default="move")
    coordinate: GantryCoordinate

class IdleCommand(BaseModel):
    command: Literal["idle"] = Field(default="idle")

class GrabBoxCommand(BaseModel):
    command: Literal["grab_box"] = Field(default="grab_box")
    coordinate: GantryCoordinate
    width: float

class PlaceBoxCommand(BaseModel):
    command: Literal["place_box"] = Field(default="place_box")
    coordinate: GantryCoordinate
    width: float

class MoveToPickupLocationCommand(BaseModel):
    command: Literal["move_to_pickup_location"] = Field(default="move_to_pickup_location")

class DropOffItemCommand(BaseModel):
    command: Literal["drop_off_item"] = Field(default="drop_off_item")

class PickupItemCommand(BaseModel):
    command: Literal["pickup_item"] = Field(default="pickup_item")

class GrabCellCommand(BaseModel):
    command: Literal["grab_cell"] = Field(default="grab_cell")
    location: StorageModuleCellLocation

class PlaceCellCommand(BaseModel):
    command: Literal["place_cell"] = Field(default="place_cell")
    location: StorageModuleCellLocation

class StoreItemCommand(BaseModel):
    command: Literal["store_item"] = Field(default="store_item")
    location: StorageModuleCellLocation

class RetrieveItemCommand(BaseModel):
    command: Literal["retrieve_item"] = Field(default="retrieve_item")
    location: StorageModuleCellLocation

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
    ],
    Field(discriminator="command")
]

# Define response types
class RobotCommandSuccessful(BaseModel):
    message: Literal["success"] = "success"

class RobotCommandError(BaseModel):
    error: str

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError]


class RobotConfiguration(BaseModel):
    tool: ToolConfiguration
    gantry: GantryConfiguration
    y_safe: float
    idle_position: GantryCoordinate
    place_offset: GantryCoordinate
    pickup_location: GantryCoordinate
    pickup_location_offset: GantryCoordinate


class RobotStatus(BaseModel):
    tool: ToolConfiguration
    gantry: GantryConfiguration
    y_safe: float
    idle_position: GantryCoordinate
    place_offset: GantryCoordinate
    pickup_location: GantryCoordinate
    pickup_location_offset: GantryCoordinate


class Robot:
    def __init__(self, bus: CANBusHandler, configuration: RobotConfiguration) -> None:
        self._configuration = configuration
        self._gantry = Gantry(bus, self._configuration.gantry)
        self._tool = Tool(bus, self._configuration.tool)
        self._storage = StorageGrid(left_module=[], right_module=[])

    @property
    def configuration(self) -> RobotConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: RobotConfiguration) -> None:
        self._configuration = configuration
        await self.initialize()

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

    async def home(self) -> None:
        await self._tool.home()
        await self._gantry.home()

    async def unsafe_move(self, coordinate: GantryCoordinate) -> None:
        await self._gantry.move_coordinate(coordinate, True)

    async def move(self, coordinate: GantryCoordinate) -> None:
        await self._tool.retract_z()
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

    async def grab_box(self, coordinate: GantryCoordinate, width: float) -> None:
        await self.move(coordinate)
        await self._tool.grab_box(width)

    async def place_box(self, coordinate: GantryCoordinate, width: float) -> None:
        await self.move(coordinate + self._configuration.place_offset)
        await self._tool.place_box(width)

    async def move_to_pickup_location(self) -> None:
        await self.move(self._configuration.pickup_location)

    async def drop_off_item(self) -> None:
        await self.move(self._configuration.pickup_location + self._configuration.place_offset)
        await self._tool.extend()
        await self.unsafe_move(
            self._configuration.pickup_location + self._configuration.pickup_location_offset
        )
        await self._tool.retract_z()

    async def pickup_item(self) -> None:
        await self.move(
            self._configuration.pickup_location + self._configuration.pickup_location_offset
        )
        await self._tool.extend()
        await self.unsafe_move(self._configuration.pickup_location)
        await self._tool.retract_z()

    async def _grab_cell(self, coordinate: GantryCoordinate, cell: Cell) -> None:
        await self.move(coordinate)
        await self._tool.grab_box(cell.dimensions.width)

    async def _place_cell(self, coordinate: GantryCoordinate, cell: Cell) -> None:
        await self.move(coordinate + self._configuration.place_offset)
        await self._tool.place_box(cell.dimensions.width)


    async def grab_cell(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_cell_coordinate(location)
        cell = self._storage.get_cell(location)
        await self._grab_cell(coordinate.to_gantry_coordinate(), cell)

    async def place_cell(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_cell_coordinate(location)
        cell = self._storage.get_cell(location)
        await self._place_cell(coordinate.to_gantry_coordinate(), cell)


    async def store_item(self, location: StorageModuleCellLocation) -> None:
        await self.grab_cell(location)
        await self.pickup_item()
        await self.place_cell(location)

    async def retrieve_item(self, location: StorageModuleCellLocation) -> None:
        await self.grab_cell(location)
        await self.drop_off_item()
        await self.place_cell(location)

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
                await self.grab_box(coord, width)
                return RobotCommandSuccessful()
            case PlaceBoxCommand(coordinate=coord, width=width):
                await self.place_box(coord, width)
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
            case StoreItemCommand(location=loc):
                await self.store_item(loc)
                return RobotCommandSuccessful()
            case RetrieveItemCommand(location=loc):
                await self.retrieve_item(loc)
                return RobotCommandSuccessful()
            case _:
                return RobotCommandError(error="Invalid Robot Command")
