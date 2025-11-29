from pydantic import BaseModel, ValidationError, model_validator, field_validator, Field
from enum import Enum
from typing import Optional, List, Self, Tuple, Mapping, Iterable, Union, Dict
from perpetual_os1.common import AxisCoordinate
import os

type Json = Dict[str, Union[str, int, float, Mapping, Iterable]]

STORAGE_PATH = os.path.join(os.path.dirname(__file__), "storage.json")

class StorageModuleSide(str, Enum):
    RIGHT = "right_module"
    LEFT = "left_module"


class CalibrationCoordinate(AxisCoordinate):


    def __add__(self, rhs: "CalibrationCoordinate") -> "CalibrationCoordinate":  # type: ignore[override]
        self._assert_same_side(rhs, "add")
        return CalibrationCoordinate(x=self.x + rhs.x, y=self.y + rhs.y, z_offset=self.z_offset + rhs.z_offset, s=self.s)

    def __sub__(self, rhs: "CalibrationCoordinate") -> "CalibrationCoordinate":  # type: ignore[override]
        self._assert_same_side(rhs, "subtract")
        return CalibrationCoordinate(x=self.x - rhs.x, y=self.y - rhs.y, z_offset=self.z_offset - rhs.z_offset, s=self.s)

    def to_axis_coordinate(self) -> AxisCoordinate:
        return AxisCoordinate(x=self.x, y=self.y, s=self.s, z_offset=self.z_offset)


class Dimensions(BaseModel):
    length: float
    width: float
    height: float


class CellStatus(str, Enum):
    OCCUPIED = "occupied"
    AVAILABLE = "available"
    MAINTENANCE = "maintenance"
    DISABLED = "disabled"


class CellItem(BaseModel):
    weight: Optional[float] = None
    dimensions: Dimensions
    data: Optional[Json] = None
    upca: Optional[str] = None


class Cell(BaseModel):
    status: CellStatus
    item: Optional[CellItem]
    calibration_point: Optional[CalibrationCoordinate]
    dimensions: Dimensions
    empty: bool = False

    def clear(self) -> None:
        self.status = CellStatus.AVAILABLE
        self.item = None


class StorageModuleCellLocation(BaseModel):
    side: StorageModuleSide
    shelf_idx: int
    cell_idx: int


class Shelf(BaseModel):
    shelf_height: float
    shelf_width: float
    cells: List[Cell]
    divider_width: float = 8.00

    @model_validator(mode="after")
    def verify_tray_heights(self) -> Self:
        if all([self.shelf_height >= cell.dimensions.height for cell in self.cells]):
            return self
        raise ValidationError("All Trays must be less than the height of the containing shelf")

    def get_cell_location(self, location: int) -> CalibrationCoordinate:
        cal_point = self.cells[location].calibration_point
        if cal_point:
            return cal_point
        else:
            cal_point = self.cells[0].calibration_point
            if cal_point is None:
                raise Exception("Invalid Calibration Point")
            else:
                x_value = cal_point.x
                cal_point = cal_point.copy()
                cal_idx = 1
                while cal_idx <= location:
                    if self.cells[cal_idx].dimensions.width == 50:
                        x_value += 61.0
                    elif self.cells[cal_idx].dimensions.width == 100:
                        x_value += 111.0
                    else:
                        x_value += 156.0
                    cal_idx += 1
                cal_point.x = x_value
                return cal_point

    def get_next_optimal_slot(self, item: CellItem) -> Optional[int]:
        cell_idx = 0
        locations = []
        while cell_idx < len(self.cells):
            if (
                self.cells[cell_idx].dimensions.width >= item.dimensions.width
                and self.cells[cell_idx].dimensions.height >= item.dimensions.height
            ):
                if self.cells[cell_idx].status == CellStatus.AVAILABLE:
                    locations.append(cell_idx)
            cell_idx += 1
        for location in locations:
            if item.dimensions.width <= 50:
                if self.cells[location].dimensions.width == 50:
                    return location
            elif item.dimensions.width <= 100:
                if self.cells[location].dimensions.width == 100:
                    return location
            else:
                return location
        return None

    def get_next_free_slot(self, item: CellItem) -> Optional[int]:
        cell_idx = 0
        while cell_idx < len(self.cells):
            if (
                self.cells[cell_idx].dimensions.width > item.dimensions.width
                and self.cells[cell_idx].dimensions.height >= item.dimensions.height
            ):
                if self.cells[cell_idx].status == CellStatus.AVAILABLE:
                    return cell_idx
            cell_idx += 1
        return None


class CellLocationData(BaseModel):
    cell: Optional[CellItem]
    location: StorageModuleCellLocation


class StorageGrid(BaseModel):
    left_module: List[Shelf] = Field(default_factory=list)
    right_module: List[Shelf] = Field(default_factory=list)
    # filename: str = "./storage.json"
    filename: str = STORAGE_PATH

    @field_validator("filename", mode="before")
    @classmethod
    def _force_storage_path(cls, _: object) -> str:
        return STORAGE_PATH

    def save_to_file(self, filename: str) -> None:
        with open(filename, "w+") as f:
            f.write(self.model_dump_json())

    def save(self) -> None:
        self.save_to_file(self.filename)

    def update(self, updates: "StorageGrid") -> None:
        self.left_module = updates.left_module.copy()
        self.right_module = updates.right_module.copy()
        self.save()

    def get_next_free_location(self, item: CellItem) -> Optional[StorageModuleCellLocation]:
        left_shelves: List[StorageModuleCellLocation] = []
        for ix, shelf in enumerate(self.left_module):
            location = shelf.get_next_optimal_slot(item)
            if location:
                left_shelves.append(
                    StorageModuleCellLocation(
                        side=StorageModuleSide.LEFT,
                        shelf_idx=ix,
                        cell_idx=location,
                    )
                )

        right_shelves: List[StorageModuleCellLocation] = []
        for ix, shelf in enumerate(self.right_module):
            location = shelf.get_next_optimal_slot(item)
            if location:
                right_shelves.append(
                    StorageModuleCellLocation(
                        side=StorageModuleSide.RIGHT,
                        shelf_idx=ix,
                        cell_idx=location,
                    )
                )

        cell_locations: List[StorageModuleCellLocation] = left_shelves + right_shelves
        for cell_location in cell_locations:
            if cell_location.side == StorageModuleSide.LEFT:
                if (
                    item.dimensions.width <= 50
                    and self.left_module[cell_location.shelf_idx]
                    .cells[cell_location.cell_idx]
                    .dimensions.width
                    == 50
                ):
                    return cell_location
                elif (
                    item.dimensions.width <= 100
                    and self.left_module[cell_location.shelf_idx]
                    .cells[cell_location.cell_idx]
                    .dimensions.width
                    == 100
                ):
                    return cell_location
                else:
                    return cell_location
            else:
                if (
                    item.dimensions.width <= 50
                    and self.right_module[cell_location.shelf_idx]
                    .cells[cell_location.cell_idx]
                    .dimensions.width
                    == 50
                ):
                    return cell_location
                elif (
                    item.dimensions.width <= 100
                    and self.right_module[cell_location.shelf_idx]
                    .cells[cell_location.cell_idx]
                    .dimensions.width
                    == 100
                ):
                    return cell_location
                else:
                    return cell_location
        return None

    def get_tray(self, location: StorageModuleCellLocation) -> Cell:
        return self.get_side(location.side)[location.shelf_idx].cells[location.cell_idx]

    def get_side(self, side: StorageModuleSide) -> List[Shelf]:
        if side == StorageModuleSide.RIGHT:
            return self.right_module
        else:
            return self.left_module

    def get_cell(self, location: StorageModuleCellLocation) -> Cell:
        if location.side == StorageModuleSide.LEFT:
            return self.left_module[location.shelf_idx].cells[location.cell_idx]
        else:
            return self.right_module[location.shelf_idx].cells[location.cell_idx]

    def get_cell_coordinate(self, location: StorageModuleCellLocation) -> CalibrationCoordinate:
        if location.side == StorageModuleSide.LEFT:
            return self.left_module[location.shelf_idx].get_cell_location(location.cell_idx)
        else:
            return self.right_module[location.shelf_idx].get_cell_location(location.cell_idx)

    def store_item(self, location: StorageModuleCellLocation, item: Optional[CellItem]) -> None:
        cell = self.get_side(location.side)[location.shelf_idx].cells[location.cell_idx]
        cell.status = CellStatus.OCCUPIED
        cell.item = item

    def clear_item(self, location: StorageModuleCellLocation) -> None:
        self.get_side(location.side)[location.shelf_idx].cells[location.cell_idx].clear()

    def clear_all_items(self) -> None:
        for shelf_idx, shelf in enumerate(self.left_module):
            for cell_idx, tray in enumerate(shelf.cells):
                self.left_module[shelf_idx].cells[cell_idx].clear()
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, tray in enumerate(shelf.cells):
                self.right_module[shelf_idx].cells[cell_idx].clear()

    def get_all_items(self) -> List[StorageModuleCellLocation]:
        items = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.OCCUPIED:
                    items.append(
                        StorageModuleCellLocation(
                            shelf_idx=shelf_idx,
                            cell_idx=cell_idx,
                            side=StorageModuleSide.LEFT,
                        )
                    )
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.OCCUPIED:
                    items.append(
                        StorageModuleCellLocation(
                            shelf_idx=shelf_idx,
                            cell_idx=cell_idx,
                            side=StorageModuleSide.RIGHT,
                        )
                    )
        return items

    def get_all_free_cells(self) -> List[StorageModuleCellLocation]:
        locations: List[StorageModuleCellLocation] = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.AVAILABLE:
                    locations.append(
                        StorageModuleCellLocation(
                            shelf_idx=shelf_idx,
                            cell_idx=cell_idx,
                            side=StorageModuleSide.LEFT,
                        )
                    )
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.AVAILABLE:
                    locations.append(
                        StorageModuleCellLocation(
                            shelf_idx=shelf_idx,
                            cell_idx=cell_idx,
                            side=StorageModuleSide.RIGHT,
                        )
                    )
        return locations

    def get_all_cells(self) -> List[Cell]:
        cells: List[Cell] = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for cell_idx, cell in enumerate(shelf.cells):
                cells.append(cell)
                    
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, cell in enumerate(shelf.cells):
                cells.append(cell)

        return cells


    def get_all_cell_data(self) -> List[CellLocationData]:
        items: List[CellLocationData] = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.OCCUPIED:
                    items.append(
                        CellLocationData(
                            cell=cell.item,
                            location=StorageModuleCellLocation(
                                shelf_idx=shelf_idx,
                                cell_idx=cell_idx,
                                side=StorageModuleSide.LEFT,
                            ),
                        )
                    )
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.OCCUPIED:
                    items.append(
                        CellLocationData(
                            cell=cell.item,
                            location=StorageModuleCellLocation(
                                shelf_idx=shelf_idx,
                                cell_idx=cell_idx,
                                side=StorageModuleSide.RIGHT,
                            ),
                        )
                    )
        return items

    def find_best_slot_for_item(self, item: CellItem) -> Optional[StorageModuleCellLocation]:
        possible_slots: List[Tuple[StorageModuleCellLocation, float]] = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.AVAILABLE and (
                    cell.dimensions.width >= item.dimensions.width
                    and cell.dimensions.length >= item.dimensions.length
                    and cell.dimensions.height >= item.dimensions.height
                ):
                    unused_space = (
                        cell.dimensions.width * cell.dimensions.height
                        - item.dimensions.width * item.dimensions.height
                    )
                    possible_slots.append(
                        (
                            StorageModuleCellLocation(
                                side=StorageModuleSide.LEFT,
                                shelf_idx=shelf_idx,
                                cell_idx=cell_idx,
                            ),
                            unused_space,
                        )
                    )
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.AVAILABLE and (
                    cell.dimensions.width >= item.dimensions.width
                    and cell.dimensions.length >= item.dimensions.length
                    and cell.dimensions.height >= item.dimensions.height
                ):
                    unused_space = (
                        cell.dimensions.width * cell.dimensions.height
                        - item.dimensions.width * item.dimensions.height
                    )
                    possible_slots.append(
                        (
                            StorageModuleCellLocation(
                                side=StorageModuleSide.RIGHT,
                                shelf_idx=shelf_idx,
                                cell_idx=cell_idx,
                            ),
                            unused_space,
                        )
                    )
        possible_slots.sort(key=lambda x: x[1])
        return possible_slots[0][0] if possible_slots else None
    
# returns item coordinates
    async def find_item_location(
        self, 
        item_name: Optional[str] = None,
        upca: Optional[str] = None,
        cell_location: Optional[StorageModuleCellLocation] = None
    ) -> Optional[AxisCoordinate]:
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
        print("finding")
            
        # # If we have a specific cell location, use that directly
        if cell_location:
            cal_coord = self.get_cell_coordinate(cell_location)
            return cal_coord.to_axis_coordinate()
        
        # # Search through all cells for matching item
        all_cells = self.get_all_cell_data()

        for cell_data in all_cells:
            if not cell_data.cell:  # Skip empty cells
                continue

            # Match by UPCA if provided
            if upca and cell_data.cell.upca == upca:
                cal_coord = self.get_cell_coordinate(cell_data.location)
                return cal_coord.to_axis_coordinate()

            # Match by item name in data
            if item_name and cell_data.cell.data:
                item_data = cell_data.cell.data
                if isinstance(item_data, dict) and item_data.get("name") == item_name:
                    cal_coord = self.get_cell_coordinate(cell_data.location)
                    return cal_coord.to_axis_coordinate()

        return None

    async def find_next_available_cell(
        self,
        width: float,
        height: float,
        length: float = 400.0
    ) -> Optional[AxisCoordinate]:
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
        # if not self._grid:
        #     return None
            
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
            return cal_coord.to_axis_coordinate()
            
        return None
    
    async def calculate_storage_positions(
        self,
        shelf_y_positions: List[float],
        cells_per_shelf: List[int],
        cell_spacings: List[float],
        x_anchor_points: List[float]
    ) -> List[List[AxisCoordinate]]:
        """Calculate positions for all storage cells based on detected shelf positions."""
        if not (len(shelf_y_positions) == len(cells_per_shelf) == len(cell_spacings) == len(x_anchor_points)):
            raise ValueError("All input lists must have the same length (one entry per shelf)")
            
        shelf_positions: List[List[AxisCoordinate]] = []
        
        for shelf_idx, (y_pos, num_cells, spacing, x_anchor) in enumerate(
            zip(shelf_y_positions, cells_per_shelf, cell_spacings, x_anchor_points)
        ):
            shelf_cells: List[AxisCoordinate] = []
            
            for cell_idx in range(num_cells):
                x_pos = x_anchor + (cell_idx * spacing)
                cell_coord = AxisCoordinate(
                    x=round(x_pos, 4),
                    y=round(y_pos, 4)
                )
                shelf_cells.append(cell_coord)
            
            shelf_positions.append(shelf_cells)
            
        return shelf_positions
