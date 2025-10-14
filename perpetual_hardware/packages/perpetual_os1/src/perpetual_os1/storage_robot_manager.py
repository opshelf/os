from typing import Optional, Dict, List, Tuple
from pydantic import BaseModel
import json
from pathlib import Path
import asyncio

from perpetual_os1.storage import (
    StorageGrid, 
    CellItem, 
    StorageModuleCellLocation,
    CellStatus,
    Dimensions,
    StorageModuleSide,
    CalibrationCoordinate
)
from perpetual_os1.robot import (
    Robot,
    RobotCommands,
    MoveCommand,
    RobotResponse,
    RobotCommandSuccessful,
    RobotCommandError
)
from perpetual_os1.common import AxisCoordinate


class StorageAdapter:
    """
    Manages the interaction between the storage grid and robot system.
    Handles storage operations while maintaining the storage state.
    """
    
    def __init__(self, storage_file: str = "storage.json", config_file: str = "config.json"):
        self.robot = Robot()
        self.storage_file = Path(storage_file)
        self.config_file = Path(config_file)
        self.storage: Optional[StorageGrid] = None
        self._load_storage()

    def _load_storage(self) -> None:
        """Load the storage grid from file or create new if not exists."""
        try:
            if self.storage_file.exists():
                with open(self.storage_file, 'r') as f:
                    data = json.load(f)
                    self.storage = StorageGrid.model_validate(data)
            else:
                # Initialize empty storage grid
                self.storage = StorageGrid(
                    left_module=[],
                    right_module=[],
                    filename=str(self.storage_file)
                )
                self.storage.save()
        except Exception as e:
            raise RuntimeError(f"Failed to load storage configuration: {e}")

    async def initialize(self) -> None:
        """Initialize the robot and storage system."""
        await self.robot.configure()
        if not self.storage:
            self._load_storage()

    # async def store_item(self, item: CellItem) -> RobotResponse:
    #     """
    #     Store an item in the storage grid using the robot.
        
    #     1. Find optimal storage location
    #     2. Move robot to IO box
    #     3. Pick up item
    #     4. Move to storage location
    #     5. Place item
    #     6. Update storage grid state
    #     """
    #     if not self.storage:
    #         return RobotCommandError(error="Storage grid not initialized")

    #     # Find best storage location
    #     location = self.storage.find_best_slot_for_item(item)
    #     if not location:
    #         return RobotCommandError(error="No suitable storage location found")

    #     try:
    #         # Get coordinates for the storage location
    #         target_coord = self.storage.get_cell_coordinate(location)

    #         # Move to IO box and pick up item
    #         await self.robot.io_pick(AxisCoordinate(x=target_coord.x, y=target_coord.y))
            
    #         # Update storage grid
    #         self.storage.store_item(location, item)
    #         self.storage.save()

    #         # Move robot to idle position
    #         await self.robot.idle()

    #         return RobotCommandSuccessful()

    #     except Exception as e:
    #         return RobotCommandError(error=f"Failed to store item: {str(e)}")

    # async def retrieve_item(self, location: StorageModuleCellLocation) -> RobotResponse:
    #     """
    #     Retrieve an item from the storage grid using the robot.
        
    #     1. Verify item exists at location
    #     2. Move robot to storage location
    #     3. Pick up item
    #     4. Move to IO box
    #     5. Place item
    #     6. Update storage grid state
    #     """
    #     if not self.storage:
    #         return RobotCommandError(error="Storage grid not initialized")

    #     cell = self.storage.get_cell(location)
    #     if cell.status != CellStatus.OCCUPIED:
    #         return RobotCommandError(error="No item at specified location")

    #     try:
    #         # Get coordinates for the storage location
    #         source_coord = self.storage.get_cell_coordinate(location)

    #         # Move to storage location and retrieve item
    #         await self.robot.io_place(AxisCoordinate(x=source_coord.x, y=source_coord.y))
            
    #         # Update storage grid
    #         self.storage.clear_item(location)
    #         self.storage.save()

    #         # Move robot to idle position
    #         await self.robot.idle()

    #         return RobotCommandSuccessful()

    #     except Exception as e:
    #         return RobotCommandError(error=f"Failed to retrieve item: {str(e)}")

    async def calibrate_cell(self, location: StorageModuleCellLocation, coordinate: AxisCoordinate) -> RobotResponse:
        """Calibrate the robot coordinates for a specific cell."""
        if not self.storage:
            return RobotCommandError(error="Storage grid not initialized")

        try:
            cell = self.storage.get_cell(location)
            cell.calibration_point = CalibrationCoordinate(x=coordinate.x, y=coordinate.y, z=0.0)  # Z can be adjusted if needed
            self.storage.save()
            return RobotCommandSuccessful()
        except Exception as e:
            return RobotCommandError(error=f"Failed to calibrate cell: {str(e)}")

    # async def verify_calibration(self, sample_points: int = 2) -> RobotResponse:
    #     """
    #     Verify storage calibration by checking a sample of cell positions.
    #     Moves robot to selected cells and verifies position using tool camera.
    #     """
    #     if not self.storage:
    #         return RobotCommandError(error="Storage grid not initialized")

    #     try:
    #         # Get all cell locations
    #         all_cells = self.storage.get_all_free_cells()
    #         if not all_cells:
    #             return RobotCommandError(error="No cells available for calibration verification")

    #         # Select sample cells to verify
    #         verify_cells = all_cells[::max(1, len(all_cells) // sample_points)]
            
    #         for location in verify_cells:
    #             # Get stored calibration coordinate
    #             stored_coord = self.storage.get_cell_coordinate(location)
                
    #             # Move to position
    #             await self.robot._tool.move(AxisCoordinate(x=stored_coord.x, y=stored_coord.y))
                
    #             # Verify position using camera
    #             frame = await self.robot._tool._cam.capture()
    #             frame = self.robot._tool._cam.correct(frame)
    #             rect = self.robot._tool._cam.detect_white_squares(frame)
                
    #             if rect is not None:
    #                 dx, dy = self.robot._tool._cam.get_center(frame, rect)
    #                 if abs(dx) > 2.0 or abs(dy) > 2.0:  # 2mm threshold, adjust as needed
    #                     return RobotCommandError(
    #                         error=f"Calibration verification failed for cell {location}: "
    #                              f"offset detected dx={dx:.2f}mm, dy={dy:.2f}mm"
    #                     )

    #         return RobotCommandSuccessful()

    #     except Exception as e:
    #         return RobotCommandError(error=f"Failed to verify calibration: {str(e)}")

    # async def scan_storage(self) -> RobotResponse:
    #     """
    #     Perform a scan of the entire storage system to verify/update status.
    #     This can include:
    #     - Visual inspection of cells
    #     - Verification of occupied/empty status
    #     - Detection of misplaced items
    #     """
    #     if not self.storage:
    #         return RobotCommandError(error="Storage grid not initialized")

    #     try:
    #         # Get all cell locations
    #         all_cells = (
    #             [(StorageModuleSide.LEFT, i, j) for i in range(len(self.storage.left_module)) 
    #              for j in range(len(self.storage.left_module[i].cells))] +
    #             [(StorageModuleSide.RIGHT, i, j) for i in range(len(self.storage.right_module)) 
    #              for j in range(len(self.storage.right_module[i].cells))]
    #         )

    #         for side, shelf_idx, cell_idx in all_cells:
    #             location = StorageModuleCellLocation(
    #                 side=side,
    #                 shelf_idx=shelf_idx,
    #                 cell_idx=cell_idx
    #             )
    #             coordinate = self.storage.get_cell_coordinate(location)
                
    #             # Move to cell position
    #             await self.robot.handle_command(
    #                 MoveCommand(coordinate=AxisCoordinate(x=coordinate.x, y=coordinate.y))
    #             )
                
    #             # TODO: Add vision system integration here to verify cell status

    #         await self.robot.idle()
    #         return RobotCommandSuccessful()

    #     except Exception as e:
    #         return RobotCommandError(error=f"Failed to scan storage: {str(e)}")

    def get_item_location(self, item_id: str) -> Optional[StorageModuleCellLocation]:
        """Find the location of an item by its ID or UPC."""
        if not self.storage:
            return None

        for location in self.storage.get_all_items():
            cell = self.storage.get_cell(location)
            if cell.item and cell.item.upca == item_id:
                return location
        return None