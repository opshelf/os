from pydantic import BaseModel
from perpetual_os1.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_os1.storage import StorageGrid
from perpetual_os1.tool import Tool, ToolConfiguration
from perpetual_can import CANBusHandler
from typing import List


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
    def gantry(self) -> Gantry:
        return self._gantry

    @property
    def tool(self) -> Tool:
        return self._tool

    @property
    def storage(self) -> StorageGrid:
        return self._storage

    @storage.setter
    def storage(self, storage: StorageGrid) -> None:
        self._storage = storage

    async def initialize(self) -> None:
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

    async def drop_off_item(self, width: float) -> None:
        await self.move(self._configuration.pickup_location)
        await self._tool.extend()
        await self.unsafe_move(
            self._configuration.pickup_location + self._configuration.pickup_location_offset
        )
        await self._tool.retract_z()

    async def pickup_item(self, width: float) -> None:
        await self.move(
            self._configuration.pickup_location + self._configuration.pickup_location_offset
        )
        await self._tool.extend()
        await self.unsafe_move(self._configuration.pickup_location)
        await self._tool.retract_z()
