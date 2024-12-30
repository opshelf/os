from pydantic import BaseModel
from perpetual_p0c.gantry import GantryConfiguration, Gantry, GantryCoordinate, GantryCommands
from perpetual_p0c.funnel import FunnelConfiguration, Funnel, FunnelCommands
from perpetual_p0c.tool import ToolConfiguration, Tool, ToolCommands
from perpetual_p0c.io_board import IoController
from perpetual_p0c.workstation import Workstation, WorkstationConfiguration, WorkstationCommands
from perpetual_p0c.size_database import SizeDatabase
from enum import Enum
import os
import time
from perpetual_p0c.storage import (
    CellItem,
    GS1Data,
    StorageGrid,
    Shelf,
    StorageModuleCellLocation,
    Tray,
    CalibrationCoordinate,
    StorageModuleTrayLocation,
    StorageModuleSide,
)
from perpetual_can import CANBusHandler
import asyncio
import os
from pydantic import BaseModel, Field, UUID4
from uuid import uuid4
from typing import Optional, Union, Literal, Annotated, List


class BottleTracking(int, Enum):
    INITIALIZE = 0x00
    WAIT_FOR_TRANSFER_TRIGGER = 0x01
    START_WORKSTATION_OUTPUT = 0x02
    WAIT_FOR_FUNNEL_OUT_TRIGGER = 0x03
    WAIT_FOR_OUTPUT_TRIGGER = 0x04
    CLOSE_DOOR = 0x05
    RESET = 0x06


class ItemTransitLocations(str, Enum):
    INPUT_CONVEYOR = "INPUT_CONVEYOR"
    INPUT_FUNNEL = "INPUT_FUNNEL"
    OUTPUT_TRANSFER_CONVEYOR = "OUTPUT_TRANSFER_CONVEYOR"
    OUTPUT_CONVEYOR = "OUTPUT_CONVEYOR"
    WORKSTATION_CONVEYOR = "WORKSTATION_CONVEYOR"


class RobotConfiguration(BaseModel):
    funnel_configuration: FunnelConfiguration
    gantry_configuration: GantryConfiguration
    tool_configuration: ToolConfiguration
    workstation: WorkstationConfiguration
    gantry_safe_x: float
    gantry_safe_y: float
    gantry_safe_y_high_clearance: float
    place_offset: GantryCoordinate
    service_coordinate_high: GantryCoordinate
    service_coordinate_low: GantryCoordinate
    idle_coordinate: GantryCoordinate
    output_location: GantryCoordinate
    input_location: GantryCoordinate
    state_file: str = "state.json"
    storage_file: str = "storage.json"
    size_file: str = "sizes.json"


class ItemStorageLocation(BaseModel):
    tray: Tray
    index: int
    shelf: int
    slot: int
    side: StorageModuleSide
    location: GantryCoordinate


class StorageOperationType(int, Enum):
    STORE = 0x01
    RETRIEVE = 0x02


class Operation(BaseModel):
    item_location: ItemStorageLocation
    operation_type: StorageOperationType


class RobotState(BaseModel):
    current_tray: Optional[StorageModuleTrayLocation]

    def clear_state(self) -> None:
        self.current_tray = None


class BaseMessage(BaseModel):
    message_id: UUID4 = Field(default_factory=uuid4)

class StorePayload:
    location: StorageModuleCellLocation
    item: Optional[CellItem]

class StoreCommand(BaseMessage):
    command: Literal["store_item"]
    payload: StorePayload

class RetrievePayload:
    location: StorageModuleCellLocation
    item: Optional[CellItem]

class RetrieveCommand(BaseMessage):
    command: Literal["retrieve_item"]
    payload: RetrievePayload 

class HomeCommand(BaseMessage):
    command: Literal["home"]

class StopCommand(BaseMessage):
    command: Literal["stop"]

class PauseCommand(BaseMessage):
    command: Literal["pause"]

class InitializeCommand(BaseMessage):
    command: Literal["initialize"]

class ToolSubsystemCommand(BaseMessage):
    command: Literal["tool_command"]
    payload: ToolCommands 

class FunnelSubsystemCommand(BaseMessage):
    command: Literal["funnel_command"]
    payload: FunnelCommands 

class WorkstationSubsystemCommand(BaseMessage):
    command: Literal["workstation_command"]
    payload: WorkstationCommands

class GantrySubsystemCommands(BaseMessage):
    command: Literal["gantry_command"]
    payload: GantryCommands


# Union of all command models
RobotCommands = Annotated[Union[
    StoreCommand, 
    RetrieveCommand, 
    HomeCommand, 
    StopCommand,
    PauseCommand,
    InitializeCommand,
    ToolSubsystemCommand,
    FunnelSubsystemCommand,
    WorkstationSubsystemCommand,
    GantrySubsystemCommands
], Field(discriminator="command")]

class P0C:
    def __init__(self, bus: CANBusHandler, config: RobotConfiguration):
        self._bus = bus
        self._storage = StorageGrid(
            left_module=[],
            right_module=[
                Shelf(
                    shelf_height=200,
                    shelf_width=800,
                    trays=[
                        Tray(
                            width=150,
                            height=200,
                            calibration_point=CalibrationCoordinate(x=85, y=24, z=480),
                            slots=[],
                        ),
                    ],
                ),
                Shelf(
                    shelf_height=200,
                    shelf_width=800,
                    trays=[
                        Tray(
                            width=100,
                            height=150,
                            calibration_point=CalibrationCoordinate(x=115, y=274, z=480),
                            slots=[],
                        ),
                        Tray(width=100, height=200, slots=[]),
                    ],
                ),
                Shelf(
                    shelf_height=200,
                    shelf_width=800,
                    trays=[
                        Tray(
                            width=50,
                            height=200,
                            calibration_point=CalibrationCoordinate(x=175, y=522, z=480),
                            slots=[],
                        ),
                        Tray(width=50, height=150, slots=[]),
                        Tray(width=50, height=100, slots=[]),
                    ],
                ),
            ],
        )
        self._config = config
        self._tool = Tool(self._bus, config.tool_configuration)
        self._funnel = Funnel(self._bus, config.funnel_configuration)
        self._gantry = Gantry(self._bus, config.gantry_configuration)
        self._io = IoController(self._bus)
        self._workstation = Workstation(self._bus, config.workstation)
        self._state = RobotState(current_tray=None)
        self._sizes = SizeDatabase()

    async def initialize(self) -> None:
        await self.gantry.initialize()
        await self.funnel.initialize()
        await self.workstation.initialize()
        await self.tool.initialize()

    def save_state(self) -> None:
        with open(self._config.state_file, "w+") as f:
            f.write(self._state.model_dump_json())

    def save_sizes(self) -> None:
        self._sizes.save_to_file(self._config.size_file)

    def load_sizes(self) -> None:
        if os.path.exists(self._config.size_file):
            self._sizes.load_from_file(self._config.size_file)
        else:
            self.save_sizes()

    def load_state(self) -> None:
        if os.path.exists(self._config.state_file):
            with open(self._config.state_file) as f:
                self._state = RobotState.model_validate_json(f.read())
        else:
            self.save_state()

    def load_storage(self) -> None:
        if os.path.exists(self._config.storage_file):
            with open(self._config.storage_file) as f:
                self._storage = StorageGrid.model_validate_json(f.read())
        else:
            self.save_storage()

    def save_storage(self) -> None:
        self._storage.save_to_file(self._config.storage_file)

    async def shutdown(self) -> None:
        await self._tool.cleanup()
        await self._gantry.cleanup()
        await self._funnel.cleanup()
        await self._workstation.cleanup()
        self._bus.shutdown()

    async def stop(self) -> None:
        await self.gantry.stop()
        await self.tool.stop()
        await self.funnel.stop()
        await self.workstation.stop()

    @property
    def config(self) -> RobotConfiguration:
        return self._config

    @property
    def gantry(self) -> Gantry:
        return self._gantry

    @property
    def funnel(self) -> Funnel:
        return self._funnel

    @property
    def tool(self) -> Tool:
        return self._tool

    @property
    def workstation(self) -> Workstation:
        return self._workstation

    @property
    def size_db(self) -> SizeDatabase:
        return self._sizes

    @property
    def storage(self) -> StorageGrid:
        return self._storage

    async def home(self) -> None:
        await self._tool.actuators(False)
        await self._workstation.home()
        await self._tool.home()
        await self._funnel.home()
        await self._gantry.home()

    async def home_y_first(self) -> None:
        await self._tool.actuators(False)
        await self._gantry.y_axis.home()
        await self._workstation.home()
        await self._tool.home()
        await self._funnel.home()
        await self._gantry.home()

    async def path_plan(self, coordinate: GantryCoordinate) -> List[GantryCoordinate]:
        gantry_position = await self.gantry.position
        path: List[GantryCoordinate] = []
        # Target is beyod safe x
        if gantry_position.x > self.config.gantry_safe_x:
            # Above High Clearance
            if gantry_position.y >= self.config.gantry_safe_y_high_clearance - 10:
                if coordinate.y >= self.config.gantry_safe_y_high_clearance - 10:
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    if coordinate.x < self.config.gantry_safe_x:
                        path.append(
                            GantryCoordinate(x=self.config.gantry_safe_x, y=gantry_position.y)
                        )
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
                    else:
                        path.append(
                            GantryCoordinate(x=self.config.gantry_safe_x, y=gantry_position.y)
                        )
                        path.append(
                            GantryCoordinate(
                                x=self.config.gantry_safe_x, y=self.config.gantry_safe_y
                            )
                        )
                        path.append(GantryCoordinate(x=coordinate.x, y=self.config.gantry_safe_y))
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
            # Below High Clearance
            else:
                if coordinate.y >= self.config.input_location.y + 10:
                    path.append(GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y))
                    path.append(
                        GantryCoordinate(x=self.config.gantry_safe_x, y=self.config.gantry_safe_y)
                    )
                    path.append(GantryCoordinate(x=self.config.gantry_safe_x, y=coordinate.y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    if coordinate.x > self.config.gantry_safe_x:
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
                    else:
                        path.append(
                            GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y)
                        )
                        path.append(
                            GantryCoordinate(
                                x=self.config.gantry_safe_x, y=self.config.gantry_safe_y
                            )
                        )
                        path.append(GantryCoordinate(x=coordinate.x, y=self.config.gantry_safe_y))
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
        else:
            if coordinate.x >= self.config.gantry_safe_x:
                if coordinate.y >= self.config.gantry_safe_y_high_clearance - 10:
                    path.append(GantryCoordinate(x=gantry_position.x, y=coordinate.y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    path.append(GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=self.config.gantry_safe_y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
            else:
                path.append(coordinate)
                return path

    async def gantry_move(self, coordinate: GantryCoordinate) -> None:
        await self.tool.actuators(False)
        tray_clear = await self._tool.check_tray_clear()
        if tray_clear:
            path = await self.path_plan(coordinate)
            await self.gantry.move_path(path)
        else:
            raise Exception("Clear Break Beams")

    async def pull_tray(self, coordinate: GantryCoordinate, tray: Tray, side: StorageModuleSide) -> None:
        tray_loaded = await self.tool.check_tray_loaded()
        if tray_loaded: 
            raise Exception("The tool is blocked from pulling a tray")
        else:
            if side == StorageModuleSide.RIGHT:
                await self.tool.safe_max()
            else:
                await self.tool.safe_min()
            await self.gantry_move(coordinate)
            await self.tool.extend_carriage(side)
            await self.tool.set_tray_solenoids(tray, side)
            # self.tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
            await asyncio.sleep(0.25)
            await self.tool.retract_carriage(side)
            if not self.tool.check_tray_loaded():
                await self.tool.set_solenoids_off()
                raise Exception("Failed Tray Pull")

    async def place_tray(self, coordinate: GantryCoordinate, side: StorageModuleSide) -> None:
        tray_loaded = await self.tool.check_tray_loaded()
        if tray_loaded: 
            await self.gantry_move(coordinate + self._config.place_offset)
            await self.tool.puller.move_to_position(self.tool.offsets.tool_extend_position - 10)
            await self.tool.puller.wait_for_move()
            await self.tool.set_solenoids_off()
            await self.tool.extend_carriage(side)
            await asyncio.sleep(0.25)
            await self.tool.carriage_safe(side)
            if not self.tool.check_tray_clear():
                raise Exception("Failed Tray Place")
            self._state.clear_state()
            self.save_state()
        else:
            raise Exception("No Tray is Loaded")

    async def move_gantry_to_input(self, tray: Tray) -> None:
        input = self.config.input_location.copy()
        input.y -= tray.cell_depth
        await self.gantry_move(input)

    async def move_gantry_to_output(self) -> None:
        await self.gantry_move(self.config.output_location)

    async def service_high(self) -> None:
        await self.gantry_move(self.config.service_coordinate_high)

    async def service_low(self) -> None:
        await self.gantry_move(self.config.service_coordinate_low)

    async def idle(self) -> None:
        await self.gantry_move(self.config.idle_coordinate)

    async def move_gantry_to_clear(self, tray: Tray) -> None:
        await self.gantry_move(
            GantryCoordinate(
                x=self._config.output_location.x,
                y=self._config.output_location.y + tray.cell_depth,
            )
        )

    async def setup_receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        await self.pull_tray(tray_location, tray, side)
        await self.move_gantry_to_input(tray)
        await self._funnel.return_diverter()
        await self._funnel.move_to_cell(tray, index)

    async def receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        await self.funnel.diverter.set_standby_current(2000)
        await self.run_input_transfer(tray, index)
        await self.setup_receive_item(tray, tray_location, index, side)
        self.save_state()
        await self._io.reset_sensors()
        await self.funnel.set_diverter_opening(tray)
        await self.funnel.diverter.set_standby_current(0)
        t0 = time.time()
        async with asyncio.timeout(10):
            while True:
                if time.time() - t0 > 10:
                    await self.funnel.stop_receive_item()
                    await self.place_tray(tray_location, side)
                    raise Exception("Failed to Receive Item")
                sensors = await self._io.get_sensors()
                if sensors and sensors.funnel_input:
                    await asyncio.sleep(2)
                    await self.funnel.close_tray_wedges(tray)
                    await self.funnel.stop_receive_item()
                    break

            time.sleep(0.1)
        await self.place_tray(tray_location, side)

    async def drop_off_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        await self.pull_tray(tray_location, tray, side)
        await self.move_gantry_to_output()
        await self.funnel.catch(tray, index)
        await self.tool.drop_items(tray, index)
        await self.funnel.move_to_transfer_location()
        await self.move_gantry_to_clear(tray)
        await self.run_output_transfer()
        await self.place_tray(tray_location, side)

    async def run_output_transfer(self) -> None:
        await self._io.reset_sensors()
        tracking = BottleTracking.INITIALIZE
        async with asyncio.timeout(20):
            while True:
                if tracking == BottleTracking.INITIALIZE:
                    await self.funnel.start_output_transfer()
                    await self.funnel.start_output()
                    await self.workstation.open_output()
                    tracking = BottleTracking.WAIT_FOR_TRANSFER_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_TRANSFER_TRIGGER:
                    sensors = await self._io.get_sensors()
                    if sensors and sensors.output_transfer_conveyor:
                        await asyncio.sleep(1)
                        await self.funnel.stop_output_transfer()
                        tracking = BottleTracking.START_WORKSTATION_OUTPUT
                elif tracking == BottleTracking.START_WORKSTATION_OUTPUT:
                    await self.workstation.start_output_belt()
                    tracking = BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER:
                    sensors = await self._io.get_sensors()
                    if sensors and sensors.output_conveyor:
                        await asyncio.sleep(1)
                        await self.funnel.stop_output()
                        tracking = BottleTracking.WAIT_FOR_OUTPUT_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_OUTPUT_TRIGGER:
                    sensors = await self._io.get_sensors()
                    if sensors and sensors.output_box:
                        await asyncio.sleep(1)
                        tracking = BottleTracking.CLOSE_DOOR
                elif tracking == BottleTracking.CLOSE_DOOR:
                    await self.workstation.close_output()
                    await self.workstation.stop_output_belt()
                    tracking = BottleTracking.RESET
                elif tracking == BottleTracking.RESET:
                    break
                await asyncio.sleep(0.1)

    async def move_gantry_to_input_location(self) -> None:
        await self.gantry_move(self.config.input_location)

    async def run_input_transfer(self, tray: Tray, idx: int) -> None:
        await self.funnel.setup_receive_item(tray, idx)
        await self._io.reset_sensors()
        await self.workstation.start_input_belt()
        await self.funnel.start_receive_item()
        async with asyncio.timeout(10):
            while True:
                sensors = await self._io.get_sensors()
                await asyncio.sleep(0.25)
                if sensors and sensors.input_conveyor:
                    await asyncio.sleep(1)
                    await self.workstation.stop_input_belt()
                    break

    async def move_to_storage_location(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_coordinate(location)
        await self.gantry_move(
            GantryCoordinate(
                x=coordinate.x,
                y=coordinate.y,
            )
        )

    async def _place_tray(self, location: StorageModuleCellLocation | StorageModuleTrayLocation) -> None:
        tray_loaded = await self.tool.check_tray_loaded()
        if tray_loaded:
            coordinate = self._storage.get_tray_coordinate(location).to_gantry_coordinate()
            await self.gantry_move(coordinate + self._config.place_offset)
            await self.tool.puller.move_to_position(self.tool.offsets.tool_extend_position - 10)
            await self.tool.puller.wait_for_move()
            await self.tool.set_solenoids_off()
            await self.tool.extend_carriage(location.side)
            await asyncio.sleep(0.25)
            await self.tool.carriage_safe(location.side)
            if not await self.tool.check_tray_clear():
                raise Exception("Failed Tray Place")
            self._state.clear_state()
            self.save_state()
        else:
            raise Exception("No Tray is Loaded")

    async def return_current_tray(self) -> None:
        if self._state.current_tray:
            await self._place_tray(self._state.current_tray)
        else:
            raise Exception("Tray Currently Held But No Known Location to Place it")

    async def _pull_tray(self, location: StorageModuleTrayLocation) -> None:
        tray_loaded = await self.tool.check_tray_loaded()
        if tray_loaded:
            if location == self._state.current_tray:
                pass
            else:
                await self.return_current_tray()
                await self._pull_tray(location)
        else:
            if location.side == StorageModuleSide.RIGHT:
                await self.tool.safe_max()
            else:
                await self.tool.safe_min()
            coordinate = self._storage.get_tray_coordinate(location)
            tray = self._storage.get_tray(location)
            await self.gantry_move(coordinate.to_gantry_coordinate())
            await self.tool.extend_carriage(location.side)
            await self.tool.set_tray_solenoids(tray, location.side)
            await asyncio.sleep(0.25)
            await self.tool.retract_carriage(location.side)
            if not await self.tool.check_tray_loaded():
                await self.tool.set_solenoids_off()
                raise Exception("Failed Tray Pull")
            self._state = RobotState(current_tray=location)
            self.save_state()

    async def _setup_receive_item(self, location: StorageModuleCellLocation) -> None:
        tray = self._storage.get_tray(location)
        await self._pull_tray(location.to_tray_location())
        await self.move_gantry_to_input(tray)

    async def _receive_item(self, location: StorageModuleCellLocation) -> None:
        tray = self._storage.get_tray(location)
        await self.funnel.diverter.set_standby_current(2000)

        #await self.run_input_transfer(tray, location.slot_idx)
        #await self._setup_receive_item(location)

        input_transfer_task = asyncio.create_task(self.run_input_transfer(tray, location.slot_idx))
        tray_pull_task = asyncio.create_task(self._setup_receive_item(location))

        await input_transfer_task
        await tray_pull_task

        await self._funnel.move_to_cell(tray, location.slot_idx)

        await self._io.reset_sensors()
        await self.funnel.set_diverter_opening(self._storage.get_tray(location))
        await self.funnel.diverter.set_standby_current(0)
        t0 = time.time()
        while True:
            if time.time() - t0 > 10:
                await self.funnel.stop_receive_item()
                await self._place_tray(location)
                raise Exception("Failed to Receive Item")
            sensors = await self._io.get_sensors()
            if sensors and sensors.funnel_input:
                await asyncio.sleep(2)
                await self.funnel.close_tray_wedges(tray)
                await self.funnel.stop_receive_item()
                break
            await asyncio.sleep(0.1)

    async def _drop_off_item(self, location: StorageModuleCellLocation) -> None:
        await self._pull_tray(location.to_tray_location())
        await self.move_gantry_to_output()
        tray = self._storage.get_tray(location)
        await self.funnel.catch(tray, location.slot_idx)
        await self.tool.drop_items(tray, location.slot_idx)
        await self.funnel.move_to_transfer_location()
        await self.move_gantry_to_clear(tray)
        await self.run_output_transfer()

    async def store_item(self, item: CellItem) -> None:
        location = self._storage.get_next_free_location(item)
        if location:
            await self._receive_item(location)
            self._storage.store_item(location, item)
            self.save_storage()
        else:
            raise Exception("No Valid Location")

    async def store_item_by_location(self, location: StorageModuleCellLocation) -> None:
        await self._receive_item(location)

    async def store_item_gs1_data(self, data: GS1Data) -> None:
        item_size = self._sizes.get_item_size(data)
        if item_size:
            item = CellItem(dimensions=item_size, gs1_data=data)
            location = self._storage.get_next_free_location(item)
            if location:
                await self._receive_item(location)
                self._storage.store_item(location, item)
                self.save_storage()
            else:
                raise Exception("No Valid Location")
        else:
            raise Exception("No Known Size")

    async def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None:
        await self._drop_off_item(location)
        self._storage.clear_item(location)
        self.save_storage()

    async def retrieve_item_by_gtin(self, data: GS1Data) -> None:
        location = self._storage.get_item_location(data)
        await self._drop_off_item(location)
        self._storage.clear_item(location)
        self.save_storage()

    async def handle_command(self, command: RobotCommands) -> None:
        match command:
            case StoreCommand(payload=payload):
                await self.store_item_by_location(payload.location)
            case RetrieveCommand(payload=payload):
                await self.retrieve_item_by_location(payload.location)
            case HomeCommand():
                await self.home()
            case StopCommand():
                await self.stop()
            case PauseCommand():
                raise NotImplementedError()
            case InitializeCommand():
                await self.initialize()
            case ToolSubsystemCommand(payload=payload):
                await self.tool.handle_command(payload)
            case FunnelSubsystemCommand(payload=payload):
                await self.funnel.handle_command(payload)
            case WorkstationSubsystemCommand(payload=payload):
                await self.workstation.handle_command(payload)
            case GantrySubsystemCommands(payload=payload):
                await self.gantry.handle_command(payload)
            case _:
                raise Exception("Invalid Robot Command")

