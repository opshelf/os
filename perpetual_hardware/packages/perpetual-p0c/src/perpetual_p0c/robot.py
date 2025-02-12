from pydantic import BaseModel
from abc import abstractmethod, ABCMeta
from perpetual_p0c.gantry import (
    GantryConfiguration,
    Gantry,
    GantryCoordinate,
    GantryCommands,
    GantryStatus,
)
from perpetual_p0c.funnel import FunnelConfiguration, Funnel, FunnelCommands, FunnelStatus
from perpetual_p0c.tool import ToolConfiguration, Tool, ToolCommands, ToolStatus
from perpetual_p0c.io_board import IoController
from perpetual_p0c.workstation import (
    Workstation,
    WorkstationConfiguration,
    WorkstationCommands,
    WorkstationStatus,
)
from perpetual_p0c.size_database import SizeDatabase
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from enum import Enum
import os
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
import logging
from pydantic import BaseModel, Field, UUID4
from uuid import uuid4
from typing import Optional, Union, Literal, Annotated, List, Dict


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


class RobotStatus(BaseModel):
    gantry_status: GantryStatus
    workstation_status: WorkstationStatus
    funnel_status: FunnelStatus
    tool_status: ToolStatus


class RobotState(BaseModel):
    current_tray: Optional[StorageModuleTrayLocation] = None

    def clear_state(self) -> None:
        self.current_tray = None


class BaseMessage(BaseModel):
    message_id: UUID4 = Field(default_factory=uuid4)


class StorePayload(BaseModel):
    location: StorageModuleCellLocation
    item: Optional[CellItem] = None


class StoreCommand(BaseMessage):
    command: Literal["store_item"] = "store_item"
    payload: StorePayload


class RetrievePayload(BaseModel):
    location: StorageModuleCellLocation
    item: Optional[CellItem] = None


class RetrieveCommand(BaseMessage):
    command: Literal["retrieve_item"] = "retrieve_item"
    payload: RetrievePayload


class HomeCommand(BaseMessage):
    command: Literal["home"] = "home"


class HomeYFirstCommand(BaseMessage):
    command: Literal["home_y_first"]


class StopCommand(BaseMessage):
    command: Literal["stop"] = "stop"


class PauseCommand(BaseMessage):
    command: Literal["pause"] = "pause"


class InitializeCommand(BaseMessage):
    command: Literal["initialize"] = "initialize"


class ReturnCurrentTray(BaseMessage):
    command: Literal["return_current_tray"] = "return_current_tray"


class PullTrayCommand(BaseMessage):
    command: Literal["pull_tray"] = "pull_tray"
    payload: StorageModuleTrayLocation


class PlaceTrayCommand(BaseMessage):
    command: Literal["place_tray"] = "place_tray"
    payload: StorageModuleTrayLocation | StorageModuleCellLocation


class ToolSubsystemCommand(BaseMessage):
    command: Literal["tool_command"] = "tool_command"
    payload: ToolCommands


class FunnelSubsystemCommand(BaseMessage):
    command: Literal["funnel_command"] = "funnel_command"
    payload: FunnelCommands


class WorkstationSubsystemCommand(BaseMessage):
    command: Literal["workstation_command"] = "workstation_command"
    payload: WorkstationCommands


class GantrySubsystemCommands(BaseMessage):
    command: Literal["gantry_command"] = "gantry_command"
    payload: GantryCommands


class IdlePositionCommand(BaseMessage):
    command: Literal["idle_position"] = "idle_position"


class ServiceHighPositionCommand(BaseMessage):
    command: Literal["service_high_position"] = "service_high_position"


class AxisHomeCommand(BaseModel):
    command: Literal["home"] = "home"


class AxisInitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"


class AxisMoveRelativeCommand(BaseModel):
    command: Literal["move_relative"] = "move_relative"
    position: float


class AxisMoveAbsoluteCommand(BaseModel):
    command: Literal["move_absolute"] = "move_absolute"
    position: float


class AxisStopCommand(BaseModel):
    command: Literal["stop"] = "stop"


AxisCommands = Annotated[
    Union[
        AxisHomeCommand,
        AxisInitializeCommand,
        AxisMoveRelativeCommand,
        AxisMoveAbsoluteCommand,
        AxisStopCommand,
    ],
    Field(discriminator="command"),
]


class AxisPayload(BaseModel):
    axis_id: int
    command: AxisCommands


class LowLevelAxisCommand(BaseMessage):
    command: Literal["axis_command"] = "axis_command"
    payload: AxisPayload


# Union of all command models
RobotCommands = Annotated[
    Union[
        StoreCommand,
        RetrieveCommand,
        HomeCommand,
        HomeYFirstCommand,
        StopCommand,
        PauseCommand,
        InitializeCommand,
        ToolSubsystemCommand,
        FunnelSubsystemCommand,
        WorkstationSubsystemCommand,
        GantrySubsystemCommands,
        ReturnCurrentTray,
        PullTrayCommand,
        PlaceTrayCommand,
        IdlePositionCommand,
        ServiceHighPositionCommand,
        LowLevelAxisCommand,
    ],
    Field(discriminator="command"),
]


class ResponseStatus(str, Enum):
    STARTING = "STARTING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


class BaseResponse(BaseMessage):
    status: ResponseStatus


class ErrorResponse(BaseResponse):
    detail: str
    command: RobotCommands


class AckResponse(BaseResponse):
    message: str
    command: RobotCommands


class StorageResponse(BaseResponse):
    command: StoreCommand


class RetrieveResponse(BaseResponse):
    command: RetrieveCommand


RobotResponses = Union[ErrorResponse, AckResponse, StorageResponse, RetrieveResponse, ErrorResponse]


class AbstractRobot(metaclass=ABCMeta):
    @abstractmethod
    async def handle_command(self, command: RobotCommands) -> RobotResponses: ...

    @property
    @abstractmethod
    def storage(self) -> StorageGrid: ...

    @storage.setter
    @abstractmethod
    def storage(self, storage: StorageGrid) -> None: ...

    @property
    @abstractmethod
    def state(self) -> RobotState: ...

    @property
    @abstractmethod
    def configuration(self) -> RobotConfiguration: ...

    @abstractmethod
    async def set_configuration(self, configuration: RobotConfiguration) -> None: ...

    @abstractmethod
    def holding_tray(self) -> bool: ...

    @abstractmethod
    async def status(self) -> Optional[RobotStatus]: ...


class P0C(AbstractRobot):
    def __init__(self, bus: CANBusHandler, configuration: RobotConfiguration):
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
        self._output_box: int = 0
        self._configuration = configuration
        self._io = IoController(self._bus)
        self._gantry = Gantry(self._bus, configuration.gantry_configuration)
        self._workstation = Workstation(self._bus, configuration.workstation)
        self._funnel = Funnel(self._bus, configuration.funnel_configuration, self._io)
        self._tool = Tool(self._bus, configuration.tool_configuration, self._io)
        self._state = RobotState(current_tray=None)
        self._sizes = SizeDatabase()

    @classmethod
    async def build(cls, bus: CANBusHandler, config: RobotConfiguration) -> "P0C":
        instance = cls(bus, config)
        await instance.initialize()
        return instance

    async def initialize(self) -> None:
        self.load_state()
        logging.info("State Loaded")
        await self.gantry.initialize()
        logging.info("Gantry Initialized")
        await self.funnel.initialize()
        logging.info("Funnel Initialized")
        await self.workstation.initialize()
        logging.info("Workstation Initialized")
        await self.tool.initialize()
        logging.info("Tool Initialized")

    def save_state(self) -> None:
        with open(self._configuration.state_file, "w+") as f:
            f.write(self._state.model_dump_json())

    def save_sizes(self) -> None:
        self._sizes.save_to_file(self._configuration.size_file)

    def load_sizes(self) -> None:
        if os.path.exists(self._configuration.size_file):
            self._sizes.load_from_file(self._configuration.size_file)
        else:
            self.save_sizes()

    def load_state(self) -> None:
        if os.path.exists(self._configuration.state_file):
            with open(self._configuration.state_file) as f:
                self._state = RobotState.model_validate_json(f.read())
        else:
            self.save_state()

    @property
    def state(self) -> RobotState:
        return self._state

    def load_storage(self) -> None:
        if os.path.exists(self._configuration.storage_file):
            with open(self._configuration.storage_file) as f:
                self._storage = StorageGrid.model_validate_json(f.read())
        else:
            self.save_storage()

    def save_storage(self) -> None:
        self._storage.save_to_file(self._configuration.storage_file)

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
    def configuration(self) -> RobotConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: RobotConfiguration) -> None:
        self._configuration = configuration
        await self._tool.set_configuration(self._configuration.tool_configuration)
        await self._gantry.set_configuration(self._configuration.gantry_configuration)
        await self._funnel.set_configuration(self._configuration.funnel_configuration)
        await self._workstation.set_configuration(self._configuration.workstation)

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

    @storage.setter
    def storage(self, storage: StorageGrid) -> None:
        self._storage = storage

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
        if gantry_position.x > self._configuration.gantry_safe_x:
            # Above High Clearance
            if gantry_position.y >= self._configuration.gantry_safe_y_high_clearance - 10:
                if coordinate.y >= self._configuration.gantry_safe_y_high_clearance - 10:
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    if coordinate.x < self._configuration.gantry_safe_x:
                        path.append(
                            GantryCoordinate(
                                x=self._configuration.gantry_safe_x, y=gantry_position.y
                            )
                        )
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
                    else:
                        path.append(
                            GantryCoordinate(
                                x=self._configuration.gantry_safe_x, y=gantry_position.y
                            )
                        )
                        path.append(
                            GantryCoordinate(
                                x=self._configuration.gantry_safe_x,
                                y=self._configuration.gantry_safe_y,
                            )
                        )
                        path.append(
                            GantryCoordinate(x=coordinate.x, y=self._configuration.gantry_safe_y)
                        )
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
            # Below High Clearance
            else:
                if coordinate.y >= self._configuration.input_location.y + 10:
                    path.append(
                        GantryCoordinate(x=gantry_position.x, y=self._configuration.gantry_safe_y)
                    )
                    path.append(
                        GantryCoordinate(
                            x=self._configuration.gantry_safe_x, y=self._configuration.gantry_safe_y
                        )
                    )
                    path.append(
                        GantryCoordinate(x=self._configuration.gantry_safe_x, y=coordinate.y)
                    )
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    if coordinate.x > self._configuration.gantry_safe_x:
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
                    else:
                        path.append(
                            GantryCoordinate(
                                x=gantry_position.x, y=self._configuration.gantry_safe_y
                            )
                        )
                        path.append(
                            GantryCoordinate(
                                x=self._configuration.gantry_safe_x,
                                y=self._configuration.gantry_safe_y,
                            )
                        )
                        path.append(
                            GantryCoordinate(x=coordinate.x, y=self._configuration.gantry_safe_y)
                        )
                        path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                        return path
        else:
            if coordinate.x >= self._configuration.gantry_safe_x:
                if coordinate.y >= self._configuration.gantry_safe_y_high_clearance - 10:
                    path.append(GantryCoordinate(x=gantry_position.x, y=coordinate.y))
                    path.append(GantryCoordinate(x=coordinate.x, y=coordinate.y))
                    return path
                else:
                    path.append(
                        GantryCoordinate(x=gantry_position.x, y=self._configuration.gantry_safe_y)
                    )
                    path.append(
                        GantryCoordinate(x=coordinate.x, y=self._configuration.gantry_safe_y)
                    )
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

    async def move_gantry_to_input(self, tray: Tray) -> None:
        input = self._configuration.input_location.copy()
        input.y -= tray.cell_depth
        await self.gantry_move(input)

    async def move_gantry_to_output(self) -> None:
        await self.gantry_move(self._configuration.output_location)

    async def service_high(self) -> None:
        await self.gantry_move(self._configuration.service_coordinate_high)

    async def service_low(self) -> None:
        await self.gantry_move(self._configuration.service_coordinate_low)

    async def idle(self) -> None:
        await self.gantry_move(self._configuration.idle_coordinate)

    async def move_gantry_to_clear(self, tray: Tray) -> None:
        await self.gantry_move(
            GantryCoordinate(
                x=self._configuration.output_location.x,
                y=self._configuration.output_location.y + tray.cell_depth,
            )
        )

    async def run_output_transfer(self, output_idx: int = 0) -> None:
        tracking = BottleTracking.INITIALIZE
        belt_shutoff_delay = 1
        async with asyncio.timeout(20):
            while True:
                if tracking == BottleTracking.INITIALIZE:
                    logging.info("Initialized")
                    await self.funnel.start_output_transfer()
                    await self.funnel.start_output()
                    await self.workstation.setup_output_boxes(output_idx)
                    tracking = BottleTracking.WAIT_FOR_TRANSFER_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_TRANSFER_TRIGGER:
                    sensors = await self._io.get_sensors()
                    if sensors and sensors.output_transfer_conveyor:
                        logging.info("Got Output Transfer")
                        await asyncio.sleep(belt_shutoff_delay)
                        await self.funnel.stop_output_transfer()
                        tracking = BottleTracking.START_WORKSTATION_OUTPUT
                elif tracking == BottleTracking.START_WORKSTATION_OUTPUT:
                    await self.workstation.start_output_belt()
                    tracking = BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER:
                    sensors = await self._io.get_sensors()
                    if sensors and sensors.output_conveyor:
                        logging.info("Got Output Sensor")
                        await asyncio.sleep(belt_shutoff_delay)
                        await self.funnel.stop_output()
                        tracking = BottleTracking.WAIT_FOR_OUTPUT_TRIGGER
                elif tracking == BottleTracking.WAIT_FOR_OUTPUT_TRIGGER:
                    sensors = await self._io.get_sensors()
                    if sensors:
                        if output_idx == 0:
                            if sensors and sensors.output_box_1:
                                logging.info("Got Door Sensor")
                                await asyncio.sleep(0.25)
                                tracking = BottleTracking.CLOSE_DOOR
                        elif output_idx == 1:
                            if sensors and sensors.output_box_2:
                                logging.info("Got Door Sensor")
                                await asyncio.sleep(0.25)
                                tracking = BottleTracking.CLOSE_DOOR
                        elif output_idx == 2:
                            if sensors and sensors.output_box_3:
                                logging.info("Got Door Sensor")
                                await asyncio.sleep(0.25)
                                tracking = BottleTracking.CLOSE_DOOR
                        else:
                            raise Exception("Invalid Output Box")
                elif tracking == BottleTracking.CLOSE_DOOR:
                    await self.workstation.close_output(output_idx + 1)
                    await self.workstation.stop_output_belt()
                    tracking = BottleTracking.RESET
                elif tracking == BottleTracking.RESET:
                    break
                await asyncio.sleep(0.1)

    async def move_gantry_to_input_location(self) -> None:
        await self.gantry_move(self._configuration.input_location)

    async def run_input_transfer(self, tray: Tray, idx: int) -> None:
        await self.funnel.setup_receive_item(tray, idx)
        await self._io.reset_sensors()
        logging.info("Sensors Reset")
        await self.funnel.start_receive_item()
        await self.workstation.start_input_belt()
        logging.info("Belt Started")
        try:
            async with asyncio.timeout(10):
                while True:
                    sensors = await self._io.get_sensors()
                    logging.info(f"Input Transfer: {sensors}")
                    if sensors and sensors.input_conveyor:
                        break
                    await asyncio.sleep(0.5)
            await asyncio.sleep(1)
            await self.workstation.stop_input_belt()
            logging.info("Input Transfer Compplete")
        except asyncio.TimeoutError:
            await self.workstation.stop_input_belt()
            await self.funnel.stop_receive_item()
            raise Exception("Failed to See Input Break Beams Trigger")
        except Exception as e:
            logging.error(f"Run Input Transfer Failure: {e}")
            raise e

    async def move_to_storage_location(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_coordinate(location)
        await self.gantry_move(
            GantryCoordinate(
                x=coordinate.x,
                y=coordinate.y,
            )
        )

    async def place_tray(
        self, location: StorageModuleCellLocation | StorageModuleTrayLocation
    ) -> None:
        tray_loaded = await self.tool.check_tray_loaded()
        if tray_loaded:
            coordinate = self._storage.get_tray_coordinate(location).to_gantry_coordinate()
            await self.gantry_move(coordinate + self._configuration.place_offset)
            await self.tool.carriage.move_to_position(
                self.tool.configuration.tool_extend_position - 10
            )
            await self.tool.carriage.wait_for_move()
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

    def holding_tray(self) -> bool:
        return self._state.current_tray is None

    async def return_current_tray(self) -> None:
        if self._state.current_tray:
            tray_loaded = await self.tool.check_tray_loaded()
            if tray_loaded:
                await self.place_tray(self._state.current_tray)
            else:
                self._state.clear_state()
            # else:
            # raise Exception("Tray Currently Held But No Known Location to Place it")

    async def pull_tray(self, location: StorageModuleTrayLocation) -> None:
        tray_loaded = await self.tool.check_tray_loaded()
        if tray_loaded:
            if location == self._state.current_tray:
                pass
            else:
                await self.return_current_tray()
                await self.pull_tray(location)
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

    async def setup_receive_item(self, location: StorageModuleCellLocation) -> None:
        tray = self._storage.get_tray(location)
        await self.pull_tray(location.to_tray_location())
        await self.move_gantry_to_input(tray)

    async def receive_item(self, location: StorageModuleCellLocation) -> None:
        tray = self._storage.get_tray(location)
        await self.funnel.initialize()
        await self.funnel.diverter.set_standby_current(2000)
        # await self.run_input_transfer(tray, location.slot_idx)
        # await self.setup_receive_item(location)

        input_transfer_task = asyncio.create_task(self.run_input_transfer(tray, location.slot_idx))
        tray_pull_task = asyncio.create_task(self.setup_receive_item(location))
        await input_transfer_task
        await tray_pull_task
        await self._funnel.move_to_cell(tray, location.slot_idx)
        await self._io.reset_sensors()
        await self.funnel.set_diverter_opening(self._storage.get_tray(location))
        await self.funnel.diverter.set_standby_current(0)
        try:
            async with asyncio.timeout(10):
                while True:
                    sensors = await self._io.get_sensors()
                    if sensors and sensors.funnel_input:
                        await asyncio.sleep(0.5)
                        break
                    await asyncio.sleep(0.5)
            await self.funnel.close_tray_wedges(tray)
            await self.funnel.stop_receive_item()
        except asyncio.TimeoutError:
            await self.funnel.stop_receive_item()
            await self.place_tray(location)
            logging.error("Failed to Receive Item into Funnel within Ten Seconds")
            raise Exception("Failed to Receive Item into Funnel within 10 seconds")

    async def drop_off_item(
        self, location: StorageModuleCellLocation, keep_tray: bool = True
    ) -> None:
        tray = self._storage.get_tray(location)
        pull_tray_task = asyncio.create_task(self.pull_tray(location.to_tray_location()))
        funnel_catch_task = asyncio.create_task(self.funnel.catch(tray, location.slot_idx))
        await pull_tray_task
        await funnel_catch_task
        await self.move_gantry_to_output()
        await self._io.reset_sensors()
        await self.tool.drop_items(tray, location.slot_idx)
        await self.funnel.move_to_transfer_location()
        await self.move_gantry_to_clear(tray)
        if keep_tray:
            await self.run_output_transfer(self._output_box)
        else:
            transfer_task = asyncio.create_task(self.run_output_transfer(self._output_box))
            return_task = asyncio.create_task(self.return_current_tray())
            await transfer_task
            await return_task
        self._output_box += 1
        if self._output_box > 2:
            self._output_box = 0

    async def store_item(self, item: CellItem) -> None:
        location = self._storage.get_next_free_location(item)
        if location:
            await self.receive_item(location)
        else:
            raise Exception("No Valid Location")

    async def store_item_by_location(self, location: StorageModuleCellLocation) -> None:
        await self.receive_item(location)

    async def store_item_gs1_data(self, data: GS1Data) -> None:
        item_size = self._sizes.get_item_size(data)
        if item_size:
            item = CellItem(dimensions=item_size, gs1_data=data)
            location = self._storage.get_next_free_location(item)
            if location:
                await self.receive_item(location)
            else:
                raise Exception("No Valid Location")
        else:
            raise Exception("No Known Size")

    async def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None:
        await self.drop_off_item(location)
        self.save_storage()

    async def retrieve_item_by_gtin(self, data: GS1Data) -> None:
        location = self._storage.get_item_location(data)
        await self.drop_off_item(location)
        self.save_storage()

    async def status(self) -> Optional[RobotStatus]:
        gantry_status = await self._gantry.status()
        workstation_status = await self._workstation.status()
        funnel_status = await self._funnel.status()
        tool_status = await self._tool.status()
        return RobotStatus(
            gantry_status=gantry_status,
            workstation_status=workstation_status,
            funnel_status=funnel_status,
            tool_status=tool_status,
        )

    async def handle_command(self, command: RobotCommands) -> RobotResponses:
        match command:
            case StoreCommand(payload=payload):
                await self.store_item_by_location(payload.location)
                return StorageResponse(status=ResponseStatus.COMPLETED, command=command)
            case RetrieveCommand(payload=payload):
                await self.retrieve_item_by_location(payload.location)
                return RetrieveResponse(status=ResponseStatus.COMPLETED, command=command)
            case PlaceTrayCommand(payload=payload):
                await self.place_tray(payload)
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="Tray Placed", command=command
                )
            case PullTrayCommand(payload=payload):
                await self.pull_tray(payload)
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="Tray Pulled", command=command
                )
            case ReturnCurrentTray():
                await self.return_current_tray()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="Tray Returned", command=command
                )
            case HomeCommand():
                await self.home()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="homed", command=command
                )
            case HomeYFirstCommand():
                await self.home_y_first()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="homed", command=command
                )
            case StopCommand():
                await self.stop()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="stopped", command=command
                )
            case PauseCommand():
                raise NotImplementedError()
            case InitializeCommand():
                await self.initialize()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="initialized", command=command
                )
            case ToolSubsystemCommand(payload=payload):
                await self.tool.handle_command(payload)
                return AckResponse(status=ResponseStatus.COMPLETED, message="tool", command=command)
            case FunnelSubsystemCommand(payload=payload):
                await self.funnel.handle_command(payload)
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="funnel", command=command
                )
            case WorkstationSubsystemCommand(payload=payload):
                await self.workstation.handle_command(payload)
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="workstation", command=command
                )
            case GantrySubsystemCommands(payload=payload):
                await self.gantry.handle_command(payload)
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="gantry", command=command
                )
            case IdlePositionCommand():
                await self.idle()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="idle position", command=command
                )
            case ServiceHighPositionCommand():
                await self.service_high()
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="service high", command=command
                )
            case LowLevelAxisCommand(payload=payload):
                axes: Dict[int, TrinamicAxis] = {
                    100: self._gantry.x_axis,
                    101: self._gantry.y_axis,
                    102: self._tool.carriage,
                    110: self._funnel.inlet_funnel,
                    111: self._funnel.opposing_funnel,
                    112: self._funnel.slot_1,
                    113: self._funnel.slot_2,
                    114: self._funnel.slot_3,
                    137: self._workstation.output_box_1.door,
                    138: self._workstation.output_box_2.door,
                    139: self._workstation.output_box_3.door,
                }
                axis = axes.get(payload.axis_id)
                if axis:
                    match payload.command:
                        case AxisHomeCommand():
                            await axis.home()
                        case AxisInitializeCommand():
                            await axis.initialize()
                        case AxisMoveRelativeCommand(position=position):
                            await axis.move_to_relative_position(position)
                        case AxisMoveAbsoluteCommand(position=position):
                            await axis.move_to_position(position)
                        case AxisStopCommand():
                            await axis.stop()
                        case _:
                            raise Exception("Invalid Axis Command")
                else:
                    raise Exception(f"Invalid Axis ID: {payload.axis_id}")
                return AckResponse(
                    status=ResponseStatus.COMPLETED, message="service high", command=command
                )
            case _:
                raise Exception("Invalid Robot Command")

