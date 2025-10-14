import perpetual_os1.common as c

from hardware.controller import SharedModbusController as Controller
from perpetual_os1.iobox import IOBox, IOBoxStatus, SizeResult, ItemSizeLocation
from perpetual_os1.tool import Tool, ToolStatus, ToolMotionStatus, ToolItemStatus
from perpetual_os1.storage import (
    StorageGrid, Shelf, Cell, CellStatus, CalibrationCoordinate,
    Dimensions, StorageModuleSide, CellItem, StorageModuleCellLocation
)

from pathlib import Path
from enum import Enum
from typing import Dict, Optional, Tuple, Union, Literal, Annotated, List
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
        self._grid: Optional[StorageGrid] = None  # Storage grid configuration

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

        await self._io._md.home()
        await self._tool.clear_alarms()

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

    async def find_item_location(
        self, 
        item_name: Optional[str] = None,
        upca: Optional[str] = None,
        cell_location: Optional[StorageModuleCellLocation] = None
    ) -> Optional[c.AxisCoordinate]:
        """
        Find an item in the storage grid by name, UPCA code, or specific location.
        Returns the AxisCoordinate for the item's position if found.
        
        Args:
            item_name: Name or identifier stored in item data
            upca: UPCA barcode of the item
            cell_location: Specific storage cell location
            
        Returns:
            AxisCoordinate if item is found, None otherwise
        """
        if not self._grid:
            return None
            
        # If we have a specific cell location, use that directly
        if cell_location:
            cal_coord = self._grid.get_cell_coordinate(cell_location)
            return cal_coord.to_gantry_coordinate()
            
        # Search through all cells for matching item
        all_cells = self._grid.get_all_cell_data()
        
        for cell_data in all_cells:
            if not cell_data.cell:  # Skip empty cells
                continue
                
            # Match by UPCA if provided
            if upca and cell_data.cell.upca == upca:
                cal_coord = self._grid.get_cell_coordinate(cell_data.location)
                return cal_coord.to_gantry_coordinate()
                
            # Match by item name in data
            if item_name and cell_data.cell.data:
                item_data = cell_data.cell.data
                if isinstance(item_data, dict) and item_data.get("name") == item_name:
                    cal_coord = self._grid.get_cell_coordinate(cell_data.location)
                    return cal_coord.to_gantry_coordinate()
                    
        return None

    async def find_next_available_cell(
        self,
        width: float,
        height: float,
        length: float = 400.0
    ) -> Optional[c.AxisCoordinate]:
        """
        Find the next available cell that can fit an item of given dimensions,
        optimizing for minimal wasted space.
        Returns the AxisCoordinate for the cell's position.
        
        Args:
            width: Width of the item in mm
            height: Height of the item in mm
            length: Length of the item in mm (default 400.0)
            
        Returns:
            AxisCoordinate if suitable cell is found, None otherwise
        """
        if not self._grid:
            return None
            
        # Create a temporary item to use storage grid's sizing logic
        temp_item = CellItem(
            dimensions=Dimensions(
                width=width,
                height=height,
                length=length
            )
        )
        
        # Always use find_best_slot_for_item to optimize space usage
        location = self._grid.find_best_slot_for_item(temp_item)
            
        if location:
            cal_coord = self._grid.get_cell_coordinate(location)
            return cal_coord.to_gantry_coordinate()
            
        return None

    #############################
    
    async def calculate_storage_positions(
        self,
        shelf_y_positions: List[float],
        cells_per_shelf: List[int],
        cell_spacings: List[float],
        x_anchor_points: List[float]
    ) -> List[List[c.AxisCoordinate]]:
        """Calculate positions for all storage cells based on detected shelf positions."""
        if not (len(shelf_y_positions) == len(cells_per_shelf) == len(cell_spacings) == len(x_anchor_points)):
            raise ValueError("All input lists must have the same length (one entry per shelf)")
            
        shelf_positions: List[List[c.AxisCoordinate]] = []
        
        for shelf_idx, (y_pos, num_cells, spacing, x_anchor) in enumerate(
            zip(shelf_y_positions, cells_per_shelf, cell_spacings, x_anchor_points)
        ):
            shelf_cells: List[c.AxisCoordinate] = []
            
            for cell_idx in range(num_cells):
                x_pos = x_anchor + (cell_idx * spacing)
                cell_coord = c.AxisCoordinate(
                    x=round(x_pos, 4),
                    y=round(y_pos, 4)
                )
                shelf_cells.append(cell_coord)
            
            shelf_positions.append(shelf_cells)
            
        return shelf_positions

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
            cell_positions = await self.calculate_storage_positions(
                shelf_y_positions=shelf_y_positions,
                cells_per_shelf=cells_per_shelf,
                cell_spacings=cell_spacings,
                x_anchor_points=x_anchor_points,
            )

            # Generate StorageGrid structure
            left_module = []
            right_module = []

            z_offset = self.get_param("Z_OFFSET", 0.0)  # Get Z offset from config or default to 0

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

    async def store(self, item_name: str) -> None:
        """
        Store an item in the storage grid, finding the best available location.
        
        Args:
            item_name: Name identifier for the item to store
            
        Returns:
            RobotResponse indicating success or failure
        """
        if not self._grid:
            return RobotCommandError(error="Storage grid not calibrated")
            
        # Find a suitable empty location for the item
        item = CellItem(
            dimensions=Dimensions(width=50.0, height=50.0, length=400.0),  # Default dimensions
            data={"name": item_name}  # Store item name in data
        )
        location = self._grid.find_best_slot_for_item(item)
        
        if not location:
            return RobotCommandError(error="No suitable storage location found")
            
        # Get coordinate and store
        coord = self._grid.get_cell_coordinate(location)
        await self.io_place(coord.to_gantry_coordinate())
        
        # Update storage grid
        self._grid.store_item(location, item)
        self._grid.save()

    async def retrieve(self, item_name: str) -> None:
        """
        Retrieve an item from storage by its name.
        
        Args:
            item_name: Name identifier of the item to retrieve
            
        Returns:
            RobotResponse indicating success or failure
        """
        if not self._grid:
            return RobotCommandError(error="Storage grid not initialized")
            
        # Find item location
        coord = await self.find_item_location(item_name=item_name)
        
        if not coord:
            return RobotCommandError(error=f"Item '{item_name}' not found in storage")
            
        # Retrieve item
        await self.io_pick(coord)

    async def measure_item_dimensions(self, n_frames: int = 5) -> Optional[Dict[str, float]]:
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
        """
        if not self._io or not self._io._cam:
            raise RuntimeError("IOBox camera not configured")
            
        # Take measurement using AFSCam
        measure_result = await self._io._cam.measure(n_frames=n_frames)
        
        if not measure_result["ok"]:
            return None
            
        # Extract relevant measurements from aggregates
        agg = measure_result["aggregate"]
        if not agg:
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
            await self._tool.move(command.coordinate)
            return RobotCommandSuccessful()
        elif isinstance(command, IdleCommand):
            await self.idle()
            return RobotCommandSuccessful()
        elif isinstance(command, StoreCommand):
            await self.store(command.item)
            return RobotCommandSuccessful()
        elif isinstance(command, RetrieveCommand):
            await self.retrieve(command.item)
            return RobotCommandSuccessful()
        else:
            return RobotCommandError(error="Invalid Robot Command")
