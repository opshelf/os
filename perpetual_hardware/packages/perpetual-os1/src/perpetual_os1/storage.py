from pydantic import BaseModel, ValidationError, model_validator, field_validator, Field
from enum import Enum
from typing import Optional, List, Self, Tuple
from perpetual_p0c.gantry import GantryCoordinate
import re


class StorageModuleSide(str, Enum):
    RIGHT = "right_module"
    LEFT = "left_module"


class CalibrationCoordinate(GantryCoordinate):
    z: float

    def __add__(self, rhs: "CalibrationCoordinate") -> "CalibrationCoordinate":  # type: ignore[override]
        return CalibrationCoordinate(x=self.x + rhs.x, y=self.y + rhs.y, z=self.z + rhs.z)

    def __sub__(self, rhs: "CalibrationCoordinate") -> "CalibrationCoordinate":  # type: ignore[override]
        return CalibrationCoordinate(x=self.x - rhs.x, y=self.y - rhs.y, z=self.z - rhs.z)

    def to_gantry_coordinate(self) -> GantryCoordinate:
        return GantryCoordinate(x=self.x, y=self.y)


class Dimensions(BaseModel):
    length: float
    width: float
    height: float


class CellStatus(str, Enum):
    OCCUPIED = "OCCUPIED"
    EMPTY = "EMPTY"
    BLOCKED = "BLOCKED"


class CellItem(BaseModel):
    weight: Optional[float] = None
    dimensions: Dimensions


class Cell(BaseModel):
    status: CellStatus
    item: Optional[CellItem]
    calibration_point: Optional[CalibrationCoordinate]
    dimensions: Dimensions

    def clear(self) -> None:
        self.status = CellStatus.EMPTY
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
                        x_value += 70.0
                    elif self.cells[cal_idx].dimensions.width == 100:
                        x_value += 120.0
                    else:
                        x_value += 170.0
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
                if self.cells[cell_idx].status == CellStatus.EMPTY:
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
                if self.cells[cell_idx].status == CellStatus.EMPTY:
                    return cell_idx
            cell_idx += 1
        return None


class CellLocationData(BaseModel):
    cell: Optional[CellItem]
    location: StorageModuleCellLocation


class StorageGrid(BaseModel):
    left_module: List[Shelf]
    right_module: List[Shelf]
    filename: str = "storage.json"

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
                if cell.status == CellStatus.EMPTY:
                    locations.append(
                        StorageModuleCellLocation(
                            shelf_idx=shelf_idx,
                            cell_idx=cell_idx,
                            side=StorageModuleSide.LEFT,
                        )
                    )
        for shelf_idx, shelf in enumerate(self.right_module):
            for cell_idx, cell in enumerate(shelf.cells):
                if cell.status == CellStatus.EMPTY:
                    locations.append(
                        StorageModuleCellLocation(
                            shelf_idx=shelf_idx,
                            cell_idx=cell_idx,
                            side=StorageModuleSide.RIGHT,
                        )
                    )
        return locations

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
                if cell.status == CellStatus.EMPTY and (
                    cell.dimensions.width >= item.dimensions.width
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
                if cell.status == CellStatus.EMPTY and (
                    cell.dimensions.width >= item.dimensions.width
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
