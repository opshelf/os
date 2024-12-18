from pydantic import BaseModel
from perpetual_p0c.gantry import GantryConfiguration, Gantry, GantryCoordinate
from perpetual_p0c.funnel import FunnelConfiguration, Funnel
from perpetual_p0c.tool import ToolConfiguration, Tool
from perpetual_p0c.io_board import IoController
import time
import atexit
from perpetual_p0c.utils import get_bus
from typing import List, Optional
from perpetual_p0c.workstation import Workstation, WorkstationConfiguration
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


class TransferOperation(BaseModel):
    start_location: ItemTransitLocations
    terminal_location: ItemTransitLocations


class P0C:
    def __init__(self, config: RobotConfiguration):
        self._bus = get_bus()
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
        # self.save_state()
        # self.save_storage()
        self.load_state()
        self.load_storage()
        atexit.register(self.shutdown)

    def save_state(self) -> None:
        with open(self._config.state_file, "w+") as f:
            f.write(self._state.model_dump_json())

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

    def shutdown(self) -> None:
        self._tool.cleanup()
        self._gantry.cleanup()
        self._funnel.cleanup()
        self._workstation.cleanup()
        self._bus.shutdown()

    def stop(self) -> None:
        self.tool.stop()
        self.funnel.stop()
        self.gantry.stop()

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

    def home(self) -> None:
        self._workstation.home()
        self._tool.home()
        self._funnel.home()
        self._gantry.home()

    def path_plan(self, coordinate: GantryCoordinate) -> List[GantryCoordinate]:
        gantry_position = self.gantry.position
        path: List[GantryCoordinate] = []
        # Target is beyod safe x
        if gantry_position.x > self.config.gantry_safe_x:
            # Above High Clearance
            if gantry_position.y >= self.config.gantry_safe_y_high_clearance - 10:
                if coordinate.y >= gantry_position.y - 10:
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
                    path.append(GantryCoordinate(x=gantry_position.x, y=self.config.gantry_safe_y))
                    path.append(
                        GantryCoordinate(x=self.config.gantry_safe_x, y=self.config.gantry_safe_y)
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

    def gantry_move(self, coordinate: GantryCoordinate) -> None:
        self.tool.actuators(False)
        if self._tool.check_tray_clear():
            path = self.path_plan(coordinate)
            self.gantry.move_path(path)
            # for coordinate in path:
            #    self.gantry.move_coordinate(coordinate)
        else:
            raise Exception("Clear Break Beams")

    def pull_tray(self, coordinate: GantryCoordinate, tray: Tray, side: StorageModuleSide) -> None:
        if self.tool.check_tray_loaded():
            raise Exception("The tool is blocked from pulling a tray")
        else:
            if side == StorageModuleSide.RIGHT:
                self.tool.safe_max()
            else:
                self.tool.safe_min()
            self.gantry_move(coordinate)
            self.tool.extend_puller(side)
            self.tool.set_tray_solenoids(tray, side)
            # self.tool.set_carriage_solenoids(0, StorageModuleSide.RIGHT)
            time.sleep(0.25)
            self.tool.retract_puller(side)
            if not self.tool.check_tray_loaded():
                raise Exception("Failed Tray Pull")

    def place_tray(self, coordinate: GantryCoordinate, side: StorageModuleSide) -> None:
        if self.tool.check_tray_loaded():
            self.gantry_move(coordinate + self._config.place_offset)
            self.tool.puller.move_to_position(self.tool.offsets.tool_extend_position - 10)
            self.tool.puller.wait_for_move()
            self.tool.set_solenoids_off()
            self.tool.extend_puller(side)
            time.sleep(0.25)
            self.tool.puller_safe(side)
            if not self.tool.check_tray_clear():
                raise Exception("Failed Tray Place")
            self._state.clear_state()
            self.save_state()
        else:
            raise Exception("No Tray is Loaded")

    def move_gantry_to_input(self, tray: Tray) -> None:
        input = self.config.input_location.copy()
        input.y -= tray.cell_depth
        self.gantry_move(input)

    def move_gantry_to_output(self) -> None:
        self.gantry_move(self.config.output_location)

    def service_high(self) -> None:
        self.gantry_move(self.config.service_coordinate_high)

    def service_low(self) -> None:
        self.gantry_move(self.config.service_coordinate_low)

    def idle(self) -> None:
        self.gantry_move(self.config.idle_coordinate)

    def move_gantry_to_clear(self, tray: Tray) -> None:
        self.gantry_move(
            GantryCoordinate(
                x=self._config.output_location.x,
                y=self._config.output_location.y + tray.cell_depth,
            )
        )

    def setup_receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        self.pull_tray(tray_location, tray, side)
        self.move_gantry_to_input(tray)
        self._funnel.return_diverter()
        self._funnel.move_to_cell(tray, index)

    def receive_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        self.funnel.diverter.set_standby_current(2000)
        self.run_input_transfer()
        self.setup_receive_item(tray, tray_location, index, side)
        self.save_state()
        self._io.reset_sensors()
        self.funnel.set_diverter_opening(tray)
        self.funnel.diverter.set_standby_current(0)
        t0 = time.time()
        while True:
            if time.time() - t0 > 10:
                self.funnel.stop_receive_item()
                self.place_tray(tray_location, side)
                raise Exception("Failed to Receive Item")
            sensors = self._io.get_sensors()
            if sensors and sensors.funnel_input:
                time.sleep(2)
                self.funnel.close_tray_wedges(tray)
                self.funnel.stop_receive_item()
                break

            time.sleep(0.1)
        self.place_tray(tray_location, side)

    def drop_off_item(
        self,
        tray: Tray,
        tray_location: GantryCoordinate,
        index: int,
        side: StorageModuleSide,
    ) -> None:
        self.pull_tray(tray_location, tray, side)
        self.move_gantry_to_output()
        self.funnel.catch(tray, index)
        self.tool.drop_items(tray, index)
        self.funnel.move_to_transfer_location()
        self.move_gantry_to_clear(tray)
        self.run_output_transfer()
        self.place_tray(tray_location, side)

    def run_output_transfer(self) -> None:
        self._io.reset_sensors()
        tracking = BottleTracking.INITIALIZE
        t0 = time.time()
        while True:
            if tracking == BottleTracking.INITIALIZE:
                self.funnel.start_output_transfer()
                self.funnel.start_output()
                self.workstation.open_output()
                tracking = BottleTracking.WAIT_FOR_TRANSFER_TRIGGER
            elif tracking == BottleTracking.WAIT_FOR_TRANSFER_TRIGGER:
                sensors = self._io.get_sensors()
                if sensors and sensors.output_transfer_conveyor:
                    time.sleep(1)
                    self.funnel.stop_output_transfer()
                    tracking = BottleTracking.START_WORKSTATION_OUTPUT
            elif tracking == BottleTracking.START_WORKSTATION_OUTPUT:
                self.workstation.start_output_belt()
                tracking = BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER
            elif tracking == BottleTracking.WAIT_FOR_FUNNEL_OUT_TRIGGER:
                sensors = self._io.get_sensors()
                if sensors and sensors.output_conveyor:
                    time.sleep(1)
                    self.funnel.stop_output()
                    tracking = BottleTracking.WAIT_FOR_OUTPUT_TRIGGER
            elif tracking == BottleTracking.WAIT_FOR_OUTPUT_TRIGGER:
                sensors = self._io.get_sensors()
                if sensors and sensors.output_box:
                    time.sleep(1)
                    tracking = BottleTracking.CLOSE_DOOR
            elif tracking == BottleTracking.CLOSE_DOOR:
                self.workstation.close_output()
                self.workstation.stop_output_belt()
                tracking = BottleTracking.RESET
            elif tracking == BottleTracking.RESET:
                break
            time.sleep(0.1)
            if time.time() - t0 > 20:
                raise Exception("Failed to Transfer Item")

    def move_gantry_to_input_location(self) -> None:
        self.gantry_move(self.config.input_location)

    def run_input_transfer(self) -> None:
        self.funnel.return_diverter()
        self.funnel.inlet_funnel.move_to_position(0)
        self.funnel.inlet_funnel.wait_for_move()
        self._io.reset_sensors()
        self.workstation.start_input_belt()
        self.funnel.start_receive_item()
        t0 = time.time()
        while True:
            if time.time() - t0 > 10:
                raise Exception("Failed to Transfer Item")
            sensors = self._io.get_sensors()
            time.sleep(0.25)
            if sensors and sensors.input_conveyor:
                time.sleep(1)
                self.workstation.stop_input_belt()
                # self.funnel.stop_receive_item()
                break

    def run_storage_operation(self, operation: Operation) -> None:
        if operation.operation_type == StorageOperationType.STORE:
            self.receive_item(
                operation.item_location.tray,
                operation.item_location.location,
                operation.item_location.index,
                operation.item_location.side,
            )
        else:
            self.drop_off_item(
                operation.item_location.tray,
                operation.item_location.location,
                operation.item_location.index,
                operation.item_location.side,
            )

    def move_to_storage_location(self, location: StorageModuleCellLocation) -> None:
        coordinate = self._storage.get_coordinate(location)
        self.gantry_move(
            GantryCoordinate(
                x=coordinate.x,
                y=coordinate.y,
            )
        )

    def _place_tray(self, location: StorageModuleCellLocation | StorageModuleTrayLocation) -> None:
        if self.tool.check_tray_loaded():
            coordinate = self._storage.get_tray_coordinate(location).to_gantry_coordinate()
            self.gantry_move(coordinate + self._config.place_offset)
            self.tool.puller.move_to_position(self.tool.offsets.tool_extend_position - 10)
            self.tool.puller.wait_for_move()
            self.tool.set_solenoids_off()
            self.tool.extend_puller(location.side)
            time.sleep(0.25)
            self.tool.puller_safe(location.side)
            if not self.tool.check_tray_clear():
                raise Exception("Failed Tray Place")
            self._state.clear_state()
            self.save_state()
        else:
            raise Exception("No Tray is Loaded")

    def return_current_tray(self) -> None:
        if self._state.current_tray:
            self._place_tray(self._state.current_tray)
        else:
            raise Exception("Tray Currently Held But No Known Location to Place it")

    def _pull_tray(self, location: StorageModuleTrayLocation) -> None:
        if self.tool.check_tray_loaded():
            if location == self._state.current_tray:
                pass
            else:
                self.return_current_tray()
        else:
            if location.side == StorageModuleSide.RIGHT:
                self.tool.safe_max()
            else:
                self.tool.safe_min()
            coordinate = self._storage.get_tray_coordinate(location)
            tray = self._storage.get_tray(location)
            self.gantry_move(coordinate.to_gantry_coordinate())
            self.tool.extend_puller(location.side)
            self.tool.set_tray_solenoids(tray, location.side)
            time.sleep(0.25)
            self.tool.retract_puller(location.side)
            if not self.tool.check_tray_loaded():
                raise Exception("Failed Tray Pull")

    def _setup_receive_item(self, location: StorageModuleCellLocation) -> None:
        tray = self._storage.get_tray(location)
        self._pull_tray(location.to_tray_location())
        self.move_gantry_to_input(tray)
        self._funnel.return_diverter()
        self._funnel.move_to_cell(tray, location.slot_idx)

    def _receive_item(self, location: StorageModuleCellLocation) -> None:
        self.funnel.diverter.set_standby_current(2000)
        self.run_input_transfer()
        self._setup_receive_item(location)
        self._state = RobotState(current_tray=location.to_tray_location())
        self.save_state()
        tray = self._storage.get_tray(location)
        self._io.reset_sensors()
        self.funnel.set_diverter_opening(self._storage.get_tray(location))
        self.funnel.diverter.set_standby_current(0)
        t0 = time.time()
        while True:
            if time.time() - t0 > 10:
                self.funnel.stop_receive_item()
                self._place_tray(location)
                raise Exception("Failed to Receive Item")
            sensors = self._io.get_sensors()
            if sensors and sensors.funnel_input:
                time.sleep(2)
                self.funnel.close_tray_wedges(tray)
                self.funnel.stop_receive_item()
                break

            time.sleep(0.1)

    def _drop_off_item(self, location: StorageModuleCellLocation) -> None:
        self._pull_tray(location.to_tray_location())
        self.move_gantry_to_output()
        tray = self._storage.get_tray(location)
        self.funnel.catch(tray, location.slot_idx)
        self.tool.drop_items(tray, location.slot_idx)
        self.funnel.move_to_transfer_location()
        self.move_gantry_to_clear(tray)
        self.run_output_transfer()

    def store_item(self, item: CellItem) -> None:
        location = self._storage.get_next_free_location(item)
        if location:
            self._receive_item(location)
            self._storage.store_item(location, item)
            self.save_storage()
        else:
            raise Exception("No Valid Location")

    def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None:
        self._drop_off_item(location)
        self._storage.clear_item(location)

    def retrieve_item_by_gtin(self, data: GS1Data) -> None:
        location = self._storage.get_item_location(data)
        self._drop_off_item(location)
        self._storage.clear_item(location)

    # Check for slots when dropping items
    # Check for actuator positions when setting them to true or false
    # Never move with actuators extended
    # Store the Current shelf/column index in the local state
