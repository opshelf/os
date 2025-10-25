from perpetual_os1.common import AxisCoordinate, load_config_file

from hardware.controller import SharedModbusController as Controller
from perpetual_os1.iobox import IOBox, IOBoxStatus, SizeResult, ItemSizeLocation
from perpetual_os1.tool import Tool, ToolStatus, ToolMotionStatus, ToolItemStatus
from perpetual_os1.storage import (
    StorageGrid, Shelf, Cell, CellStatus, CalibrationCoordinate,
    Dimensions, StorageModuleSide, CellItem, StorageModuleCellLocation
)

from perpetual_os1.exceptions import (
    ConfigError,
)

from perpetual_os1.robot_state import RobotStateManager, RobotStateError

import json
from pathlib import Path
from enum import Enum
from typing import Dict, Optional, Tuple, Union, Literal, Annotated, List, Callable, TYPE_CHECKING
from pydantic import BaseModel, Field

import time

import os
STORAGE_PATH = os.path.join(os.path.dirname(__file__), 'storage.json')
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

class StopCommand(BaseModel):
    command: Literal["stop"] = "stop"

class InitializeCommand(BaseModel):
    command: Literal["initialize"] = "initialize"

class HomeCommand(BaseModel):
    command: Literal["home"] = "home"

class MoveCommand(BaseModel):
    command: Literal["move"] = "move"
    coordinate: AxisCoordinate

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
    location: StorageModuleCellLocation


class RobotState(BaseModel):
    io: IOBoxStatus
    tool: ToolStatus
    current_cell: Optional[StorageModuleCellLocation] = None

class RobotStatus(BaseModel):
    storage: StorageGrid
    state: RobotState
    light_curtain: bool

RobotResponse = Union[RobotCommandSuccessful, RobotCommandError, SizeResult]

class Robot:
    def __init__(self = None) -> None:
        # TODO add port retries, config, hardening
        self._motorbus = Controller(port="/dev/ttyUSB1", baudrate=115200) # change to config
        # self._scalebus = Controller(port="/dev/serial/by-id/usb-1a86_USB_Single_Serial_594D007725-if00", baudrate=9600) # change to config

        # Load State
        self._config = load_config_file(CONFIG_PATH)
        tool_params = self._config.get("robot.tool", {})
        io_params = self._config.get("robot.io", {})

        # Robot Components
        self._tool = Tool(motorcontroller=self._motorbus, params=tool_params)
        self._io = IOBox(motorcontroller=self._motorbus, params=io_params)

        # storage grid to store physical robot state
        with open(STORAGE_PATH, 'r') as f:
             data = json.load(f)
        self._grid = StorageGrid.model_validate(data)  # Storage grid configuration

        # Status
        self._cur_item: StorageModuleCellLocation = None # some kind of unique identifier
        self._status: RobotStatus = None

        self._state: RobotState = RobotState(io=IOBoxStatus.IDLE, tool=ToolStatus(motion=ToolMotionStatus.IDLE, item=ToolItemStatus.EMPTY))

        # State Machine
        self._state_manager = RobotStateManager()

    async def configure(self):
        await self._motorbus.connect()
        if not self._motorbus.client.connected:
            print("No Motor bus")
            return
        # await self._scalebus.connect()
        # if not self._scalebus.client.connected:
        #     print("No scale bus")
        #     return

        # await self._tool.configure()
        await self._io.configure()

    def get_param(self, param: str):
        try:
            if not hasattr(self._config.robot, param):
                raise KeyError(f"Parameter '{param}' not found")
            return getattr(self._config.robot, param)
        except Exception as e:
            raise ConfigError(f"Failed to get parameter {param}: {str(e)}") from e

    async def io_place(self, coord: AxisCoordinate):
        # Validate state before operation
        self._state_manager.update_state(self._state.tool, self._state.io)
        self._state_manager.validate_operation('place_iobox')

        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")

        await self._tool.pick(coord)
        await self._tool.move(AxisCoordinate(ppl[0], ppl[1])) # move to safe height
        await self._io.open_door()
        await self._tool.extend_IO()
        await self._tool.move(AxisCoordinate(pph[0], pph[1]))
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

        await self._io._md.home()
        await self._tool.clear_alarms()

        # Update state after operation
        self._state_manager.update_state(self._state.tool, self._state.io)

    async def io_pick(self, coord):
        # Validate state before operation
        self._state_manager.update_state(self._state.tool, self._state.io)
        self._state_manager.validate_operation('pick_iobox')

        ppl = await self._tool.get_param("PICKUP_POSITION_L")
        pph = await self._tool.get_param("PICKUP_POSITION_H")

        await self._tool.pick(coord)
        await self._tool.move(AxisCoordinate(pph[0], pph[1])) # move to safe height
        await self._io.open_door()
        await self._tool.extend()
        await self._tool.move(AxisCoordinate(ppl[0], ppl[1])) # move to safe height
        await self._tool.retract()
        await self._io.close_door()
        await self._tool.move(coord)
        await self._tool.place(coord)

        await self._io._md.home() # clear alarms io
        await self._tool.clear_alarms()

        # Update state after operation
        self._state_manager.update_state(self._state.tool, self._state.io)

    async def idle(self) -> None:
        await self._tool.move(self._tool.get_param("IDLE_POSITION"))

    async def shutdown(self) -> None:
        await self.stop()
        if self._motorbus:
            await self._motorbus.close()
        if self._scalebus:
            await self._scalebus.close()

    async def stop(self) -> None:
        await self._tool.stop()
        await self._io.stop()


    async def auto_calibrate_storage(self) -> RobotResponse:
        """
        Perform full storage system calibration:
        1. Find shelf positions using tool camera
        2. Detect anchor points
        3. Calculate cell positions (cells per shelf and spacing are provided per-shelf)
        4. Generate and save storage grid configuration
        """
        try:
            # Detect shelf positions and anchors first (we need number of shelves)
            shelf_y_positions = await self._tool.get_shelves()
            if not shelf_y_positions:
                return RobotCommandError(error="No shelves detected")

            num_shelves = len(shelf_y_positions)

            # Load parameters (can be a single value or a list matching shelves)
            # this is actually a function of the robot, to keep the grid defintion simple
            raw_cells_per_shelf = self.get_param("CELLS_PER_SHELF")
            raw_cell_spacings = self.get_param("CELL_SPACINGS")
            cell_heights = self.get_param("CELL_HEIGHTS")
            cell_widths = self.get_param("CELL_WIDTHS")

            if any(param is None for param in [raw_cells_per_shelf, raw_cell_spacings, cell_heights, cell_widths]):
                return RobotCommandError(error="Missing required parameters: CELLS_PER_SHELF, CELL_SPACINGS, CELL_HEIGHTS, or CELL_WIDTHS")

            # Normalize cells_per_shelf -> List[int] length == num_shelves
            if isinstance(raw_cells_per_shelf, int):
                cells_per_shelf = [raw_cells_per_shelf] * num_shelves
            elif isinstance(raw_cells_per_shelf, list):
                if len(raw_cells_per_shelf) != num_shelves:
                    return RobotCommandError(
                        error=f"CELLS_PER_SHELF length ({len(raw_cells_per_shelf)}) does not match detected shelves ({num_shelves})"
                    )
                cells_per_shelf = [int(x) for x in raw_cells_per_shelf]
            else:
                return RobotCommandError(error="CELLS_PER_SHELF must be an int or list of ints")

            # Normalize cell_spacings -> List[float] length == num_shelves
            if isinstance(raw_cell_spacings, (int, float)):
                cell_spacings = [float(raw_cell_spacings)] * num_shelves
            elif isinstance(raw_cell_spacings, list):
                if len(raw_cell_spacings) != num_shelves:
                    return RobotCommandError(
                        error=f"CELL_SPACINGS length ({len(raw_cell_spacings)}) does not match detected shelves ({num_shelves})"
                    )
                cell_spacings = [float(x) for x in raw_cell_spacings]
            else:
                return RobotCommandError(error="CELL_SPACINGS must be a number or list of numbers")

            # Detect anchors (one anchor per shelf expected)
            x_anchor_points = await self._tool.update_grid(shelf_y_positions)
            if not x_anchor_points or len(x_anchor_points) != num_shelves:
                return RobotCommandError(
                    error=f"Anchor detection returned {len(x_anchor_points) if x_anchor_points is not None else 0} anchors; expected {num_shelves}"
                )

            # Calculate all cell positions
            cell_positions = await self._grid.calculate_storage_positions(
                shelf_y_positions=shelf_y_positions,
                cells_per_shelf=cells_per_shelf,
                cell_spacings=cell_spacings,
                x_anchor_points=x_anchor_points,
            )

            # Generate StorageGrid structure
            left_module = []
            right_module = []

            # z_offset = self.get_param("Z_OFFSET", 0.0)  # Get Z offset from config or default to 0
            z_offset = 0.0

            for shelf_idx, shelf_cells in enumerate(cell_positions):
                shelf_cells_left = []
                shelf_cells_right = []

                mid_point = len(shelf_cells) // 2
                shelf_height = cell_heights[shelf_idx] if isinstance(cell_heights, list) else cell_heights

                # Process left side cells
                for cell_idx, coord in enumerate(shelf_cells[:mid_point]):
                    cell_width = cell_widths[cell_idx] if isinstance(cell_widths, list) else cell_widths
                    cell = Cell(
                        status=CellStatus.EMPTY,
                        item=None,
                        calibration_point=CalibrationCoordinate(x=coord.x, y=coord.y, z=z_offset),
                        dimensions=Dimensions(width=cell_width, height=shelf_height, length=400.0)  # Default length
                    )
                    shelf_cells_left.append(cell)

                # Process right side cells
                for cell_idx, coord in enumerate(shelf_cells[mid_point:]):
                    cell_width = cell_widths[cell_idx + mid_point] if isinstance(cell_widths, list) else cell_widths
                    cell = Cell(
                        status=CellStatus.EMPTY,
                        item=None,
                        calibration_point=CalibrationCoordinate(x=coord.x, y=coord.y, z=z_offset),
                        dimensions=Dimensions(width=cell_width, height=shelf_height, length=400.0)  # Default length
                    )
                    shelf_cells_right.append(cell)

                # Create shelf objects
                if shelf_cells_left:
                    left_module.append(Shelf(
                        shelf_height=shelf_height,
                        shelf_width=sum(cell.dimensions.width for cell in shelf_cells_left),
                        cells=shelf_cells_left
                    ))

                if shelf_cells_right:
                    right_module.append(Shelf(
                        shelf_height=shelf_height,
                        shelf_width=sum(cell.dimensions.width for cell in shelf_cells_right),
                        cells=shelf_cells_right
                    ))

            # Create and save StorageGrid
            storage_grid = StorageGrid(
                left_module=left_module,
                right_module=right_module,
                filename=str(CONFIG_PATH.parent / "storage_grid.json")
            )
            storage_grid.save()

            # Update internal grid reference
            self._grid = storage_grid

            return RobotCommandSuccessful()

        except Exception as e:
            return RobotCommandError(error=f"Storage calibration failed: {str(e)}")


    # plug in sizing from the io box
    async def store(self, item_name: str) -> None:
        """
        Store an item in the storage grid, finding the best available location based on item dimensions.

        Args:
            item_name: Name identifier for the item to store
            dimensions: Dictionary containing item dimensions in mm (from measure_item_dimensions).
                       Should contain some of these keys:
                       - footprint_major_mm: Major axis from pinhole model
                       - footprint_minor_mm: Minor axis from pinhole model
                       - scale_major_mm: Major axis from checkerboard scale
                       - scale_minor_mm: Minor axis from checkerboard scale
                       - depth_mm: Item depth/height

        Returns:
            RobotResponse indicating success or failure
        """
        if not self._grid:
            return RobotCommandError(error="Storage grid not calibrated")

        # Set default dimensions or use measured ones
        width = 50.0  # default width
        height = 50.0  # default height
        length = 50.0  # default length

        dimensions = self.measure_item_dimensions()
        print(dimensions, flush=True)

        # TODO remove dimensions, and modify io sizer wrapper to return a dimensions object

        # if dimensions:
        #     # Prefer footprint measurements over scale measurements if available
        #     if "footprint_major_mm" in dimensions and "footprint_minor_mm" in dimensions:
        #         width = dimensions["footprint_major_mm"]
        #         height = dimensions["footprint_minor_mm"]
        #     elif "scale_major_mm" in dimensions and "scale_minor_mm" in dimensions:
        #         width = dimensions["scale_major_mm"]
        #         height = dimensions["scale_minor_mm"]

        #     # Use measured depth if available
        #     if "depth_mm" in dimensions:
        #         length = dimensions["depth_mm"]

        #     # Add safety margin to ensure item fits (10%)
        #     width *= 1.1
        #     height *= 1.1
        #     length *= 1.1

        # Find a suitable empty location for the item

        print(width, flush=True)
        print(height,flush=True)
        print(length,flush=True)

        item = CellItem(
            dimensions=Dimensions(
                width=width,
                height=height,
                length=length
            ),
            data={"name": item_name}  # Store item name in data
        )

        # print(item, flush=True)
        # print("beb")
        location = self._grid.find_best_slot_for_item(item)
        print(location, flush=True)

        if not location:
            print("no cell")
            return RobotCommandError(error="No suitable storage location found")

        # Get coordinate and store
        coord = self._grid.get_cell_coordinate(location)
        print(coord)
        # await self.io_pick(coord.to_axis_coordinate())

        # Update storage grid
        self._grid.store_item(location, item)
        self._grid.save()
        print(self._grid)

    async def retrieve(self, item_name: str) -> None:
        """
        Retrieve an item from storage by its name. # todo or upca

        Args:
            item_name: Name identifier of the item to retrieve

        Returns:
            RobotResponse indicating success or failure
        """
        if not self._grid:
            return RobotCommandError(error="Storage grid not initialized")

        # Find item location
        coord = await self._grid.find_item_location(item_name=item_name)

        if not coord:
            return RobotCommandError(error=f"Item '{item_name}' not found in storage")

        # Retrieve item
        print(coord)
        # await self.io_place(coord)

        # clear the item we just retrieved
        location = next(
            (
                entry.location
                for entry in self._grid.get_all_cell_data()
                if entry.cell
                and entry.cell.data
                and isinstance(entry.cell.data, dict)
                and entry.cell.data.get("name") == item_name
            ),
            None,
        )
        if location:

            self._grid.clear_item(location)
            self._grid.save()

    def measure_item_dimensions(self, n_frames: int = 10) -> Optional[Dict[str, float]]:
        """
        Use the IOBox's AFSCam to measure the dimensions of an item currently in the IO box.
        Returns the aggregate dimensions including footprint and depth measurements.

        Args:
            n_frames: Number of frames to capture for measurement (default=5)

        Returns:
            Dictionary containing the following measurements in millimeters:
            - footprint_major_mm: Major axis of item footprint from pinhole model
            - footprint_minor_mm: Minor axis of item footprint from pinhole model
            - scale_major_mm: Major axis from checkerboard scale
            - scale_minor_mm: Minor axis from checkerboard scale
            - depth_mm: Item depth/height from TOF sensor
            Returns None if measurement fails

        Raises:
            RobotStateError: If there is no item in the IOBox to measure
        """
        # Validate state before operation -> change state on weight read
        # self._state_manager.update_state(self._state.tool, self._state.io)
        # self._state_manager.validate_operation('measure')
        if not self._io or not self._io._cam:
            raise RuntimeError("IOBox camera not configured")

        # Take measurement using AFSCam
        measure_result = self._io.measure_item_sync()
        # print(measure_result)

        if not measure_result["ok"]:
            print("bad")
            return None

        # Extract relevant measurements from aggregates
        agg = measure_result["aggregate"]
        if not agg:
            print("bad agg")
            return None

        # Collect all available measurements
        result = {}

        # Footprint measurements (from pinhole model)
        if "footprint_major_mm_med" in agg:
            result["footprint_major_mm"] = agg["footprint_major_mm_med"]
        if "footprint_minor_mm_med" in agg:
            result["footprint_minor_mm"] = agg["footprint_minor_mm_med"]

        # Scale measurements (from checkerboard)
        if "scale_major_mm_med" in agg:
            result["scale_major_mm"] = agg["scale_major_mm_med"]
        if "scale_minor_mm_med" in agg:
            result["scale_minor_mm"] = agg["scale_minor_mm_med"]

        # Depth measurement
        if "depth_mm_med" in agg:
            result["depth_mm"] = agg["depth_mm_med"]

        return result if result else None

    async def get_status(self) -> RobotStatus:
        status = RobotStatus(storage=self._grid,
                             state=RobotState(io=self._io._status,
                                              tool=self._tool._status),
                             light_curtain= False)
        self._status = status
        return status
    
    async def test(self, coordinate) -> None:
        await self.store(item_name="andi")

    async def handle_command(self, command: RobotCommands):
        if isinstance(command, StopCommand):
            await self.stop()
            return RobotCommandSuccessful()
        elif isinstance(command, InitializeCommand):
            await self.configure()
            return RobotCommandSuccessful()
        elif isinstance(command, HomeCommand):
            await self._tool.home()
            return RobotCommandSuccessful()
        elif isinstance(command, MoveCommand):
            # await self._tool.move(command.coordinate)
            await self.test(command.coordinate)
            return RobotCommandSuccessful()
        elif isinstance(command, IdleCommand):
            await self.idle() # todo remove
            return RobotCommandSuccessful()
        elif isinstance(command, StoreCommand):
            await self.store(command.item)
            return RobotCommandSuccessful()
        elif isinstance(command, RetrieveCommand):
            await self.retrieve(command.item)
            return RobotCommandSuccessful()
        else:
            return RobotCommandError(error="Invalid Robot Command")
