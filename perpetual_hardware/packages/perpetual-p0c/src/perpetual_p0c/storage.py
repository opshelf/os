from pydantic import BaseModel, ValidationError, model_validator
from enum import Enum
from typing import Optional, List, Self, Tuple
from perpetual_p0c.gantry import GantryCoordinate


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


class ItemType(str, Enum):
    BOTTLE = "BOTTLE"
    BOX = "BOX"


class GS1Data(BaseModel):
    gtin: str
    expiry: str
    lot: str
    sn: str


class CellStatus(str, Enum):
    OCCUPIED = "OCCUPIED"
    EMPTY = "EMPTY"
    BLOCKED = "BLOCKED"


class CellItem(BaseModel):
    # Width and Height are the Bounding Box of the item
    dimensions: Dimensions
    item_type: Optional[ItemType] = None
    gs1_data: Optional[GS1Data] = None
    ndc_code: Optional[str] = None
    weight: Optional[float] = None


class Cell(BaseModel):
    status: CellStatus
    item: Optional[CellItem]


class Tray(BaseModel):
    width: int
    height: float
    calibration_point: Optional[CalibrationCoordinate] = None
    empty: bool = True
    slots: List[Cell] = []
    wall_size: float = 5.0
    tray_length: float = 335.0

    @property
    def cells(self) -> int:
        return len(self.slots)

    @property
    def cell_depth(self) -> float:
        return self.height

    @property
    def cell_width(self) -> float:
        return self.width

    def get_cell_position(self, idx: int) -> Tuple[float, float]:
        cell_width = (self.tray_length - (2 + len(self.slots) - 1) * self.wall_size) / len(
            self.slots
        )
        start_position = self.wall_size + cell_width * idx + self.wall_size * idx
        if idx > 0:
            start_position += 5
            return (start_position, start_position + cell_width)
        else:
            return (start_position, start_position + cell_width + 5)

    @model_validator(mode="after")
    def verify_height(self) -> Self:
        if self.width <= self.height:
            return self
        raise ValidationError("Invalid Tray Height width must be less than or equal to the height")

    @model_validator(mode="after")
    def verify_calibration_point(self) -> Self:
        if self.calibration_point:
            if self.calibration_point.x < 0.1 and self.calibration_point.y < 0.1 and self.calibration_point.z < 0.1:
                self.calibration_point = None
        return self

    @model_validator(mode="after")
    def verify_slots(self) -> Self:
        if self.width == 50:
            if len(self.slots) == 6:
                return self
            else:
                self.slots = [Cell(status=CellStatus.EMPTY, item=None) for _ in range(0, 6)]
                return self
        elif self.width == 100:
            if len(self.slots) == 3:
                return self
            else:
                self.slots = [Cell(status=CellStatus.EMPTY, item=None) for _ in range(0, 3)]
                return self
        elif self.width == 150:
            self.slots = [Cell(status=CellStatus.EMPTY, item=None) for _ in range(0, 2)]
            return self
        else:
            raise ValidationError("Invalid Tray Width")

    def get_next_free_slot(self) -> Optional[int]:
        idx = 0
        while idx < len(self.slots):
            if self.slots[idx].status == CellStatus.EMPTY:
                return idx
            else:
                idx += 1
        return None


class ShelfCellLocation(BaseModel):
    tray_idx: int
    slot_idx: int


class ModuleCellLocation(BaseModel):
    shelf_idx: int
    tray_idx: int
    slot_idx: int


class StorageModuleTrayLocation(BaseModel):
    side: StorageModuleSide
    shelf_idx: int
    tray_idx: int


class StorageModuleCellLocation(BaseModel):
    side: StorageModuleSide
    shelf_idx: int
    tray_idx: int
    slot_idx: int

    def to_tray_location(self) -> StorageModuleTrayLocation:
        return StorageModuleTrayLocation(
            side=self.side, shelf_idx=self.shelf_idx, tray_idx=self.tray_idx
        )


class Shelf(BaseModel):
    shelf_height: float
    shelf_width: float
    trays: List[Tray]
    divider_width: float = 8.00

    @model_validator(mode="after")
    def verify_tray_heights(self) -> Self:
        if all([self.shelf_height >= tray.height for tray in self.trays]):
            return self
        raise ValidationError("All Trays must be less than the height of the containing shelf")

    def get_tray_location(self, location: ShelfCellLocation) -> CalibrationCoordinate:
        cal_point = self.trays[location.tray_idx].calibration_point
        if cal_point:
            return cal_point
        else:
            cal_point = self.trays[0].calibration_point
            if cal_point is None:
                raise Exception("Invalid Calibration Point")
            else:
                x_value = cal_point.x
                cal_point = cal_point.copy()
                cal_idx = 1
                while cal_idx <= location.tray_idx:
                    if self.trays[cal_idx].width == 50:
                        x_value += 70.0
                    elif self.trays[cal_idx].width == 100:
                        x_value += 120.0
                    else:
                        x_value += 170.0
                    cal_idx += 1
                cal_point.x = x_value
                return cal_point

    def get_next_optimal_slot(self, item: CellItem) -> Optional[ShelfCellLocation]:
        tray_idx = 0
        locations = []
        while tray_idx < len(self.trays):
            if (
                self.trays[tray_idx].width >= item.dimensions.width
                and self.trays[tray_idx].height >= item.dimensions.height
            ):
                open_slot = self.trays[tray_idx].get_next_free_slot()
                if not open_slot is None:
                    locations.append(ShelfCellLocation(tray_idx=tray_idx, slot_idx=open_slot))
            tray_idx += 1
        for location in locations:
            if item.dimensions.width <= 50:
                if self.trays[location.tray_idx].width == 50:
                    return location
            elif item.dimensions.width <= 100:
                if self.trays[location.tray_idx].width == 100:
                    return location
            else:
                return location
        return None

    def get_next_free_slot(self, item: CellItem) -> Optional[ShelfCellLocation]:
        tray_idx = 0
        while tray_idx < len(self.trays):
            if (
                self.trays[tray_idx].width > item.dimensions.width
                and self.trays[tray_idx].height >= item.dimensions.height
            ):
                open_slot = self.trays[tray_idx].get_next_free_slot()
                if open_slot:
                    return ShelfCellLocation(tray_idx=tray_idx, slot_idx=open_slot)
            tray_idx += 1
        return None


class CellLocationData(BaseModel):
    cell: CellItem
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
        for key, value in updates.dict().items():
            setattr(self, key, value)
        self.save()

    # @staticmethod
    # def load_from_file(filename: str) -> "StorageGrid":
    #    with open(filename, "r") as f:
    #        return Self.model_valiate_json(f.read())

    def get_next_free_location(self, item: CellItem) -> Optional[StorageModuleCellLocation]:
        left_shelves: List[StorageModuleCellLocation] = []
        for ix, shelf in enumerate(self.left_module):
            location = shelf.get_next_optimal_slot(item)
            if location:
                left_shelves.append(
                    StorageModuleCellLocation(
                        side=StorageModuleSide.LEFT,
                        shelf_idx=ix,
                        tray_idx=location.tray_idx,
                        slot_idx=location.slot_idx,
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
                        tray_idx=location.tray_idx,
                        slot_idx=location.slot_idx,
                    )
                )

        cell_locations: List[StorageModuleCellLocation] = left_shelves + right_shelves
        for cell_location in cell_locations:
            if cell_location.side == StorageModuleSide.LEFT:
                if (
                    item.dimensions.width <= 50
                    and self.left_module[cell_location.shelf_idx]
                    .trays[cell_location.tray_idx]
                    .width
                    == 50
                ):
                    return cell_location
                elif (
                    item.dimensions.width <= 100
                    and self.left_module[cell_location.shelf_idx]
                    .trays[cell_location.tray_idx]
                    .width
                    == 100
                ):
                    return cell_location
                else:
                    return cell_location
            else:
                if (
                    item.dimensions.width <= 50
                    and self.right_module[cell_location.shelf_idx]
                    .trays[cell_location.tray_idx]
                    .width
                    == 50
                ):
                    return cell_location
                elif (
                    item.dimensions.width <= 100
                    and self.right_module[cell_location.shelf_idx]
                    .trays[cell_location.tray_idx]
                    .width
                    == 100
                ):
                    return cell_location
                else:
                    return cell_location
        return None

    def get_tray(self, location: StorageModuleCellLocation | StorageModuleTrayLocation) -> Tray:
        return self.get_side(location.side)[location.shelf_idx].trays[location.tray_idx]

    def get_side(self, side: StorageModuleSide) -> List[Shelf]:
        if side == StorageModuleSide.RIGHT:
            return self.right_module
        else:
            return self.left_module

    def get_tray_coordinate(
        self, location: StorageModuleTrayLocation | StorageModuleCellLocation
    ) -> CalibrationCoordinate:
        if location.side == StorageModuleSide.LEFT:
            return self.left_module[location.shelf_idx].get_tray_location(
                ShelfCellLocation(tray_idx=location.tray_idx, slot_idx=0)
            )
        else:
            return self.right_module[location.shelf_idx].get_tray_location(
                ShelfCellLocation(tray_idx=location.tray_idx, slot_idx=0)
            )

    def get_coordinate(self, location: StorageModuleCellLocation) -> CalibrationCoordinate:
        if location.side == StorageModuleSide.LEFT:
            return self.left_module[location.shelf_idx].get_tray_location(
                ShelfCellLocation(tray_idx=location.tray_idx, slot_idx=location.slot_idx)
            )
        else:
            return self.right_module[location.shelf_idx].get_tray_location(
                ShelfCellLocation(tray_idx=location.tray_idx, slot_idx=location.slot_idx)
            )

    def store_item(self, location: StorageModuleCellLocation, item: CellItem) -> None:
        self.get_side(location.side)[location.shelf_idx].trays[location.tray_idx].slots[
            location.slot_idx
        ] = Cell(status=CellStatus.OCCUPIED, item=item)

    def clear_item(self, location: StorageModuleCellLocation) -> None:
        self.get_side(location.side)[location.shelf_idx].trays[location.tray_idx].slots[
            location.slot_idx
        ] = Cell(status=CellStatus.EMPTY, item=None)

    def clear_all_items(self) -> None:
        for shelf_idx, shelf in enumerate(self.left_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, _ in enumerate(tray.slots):
                    self.left_module[shelf_idx].trays[tray_idx].slots[slot_idx] = Cell(
                        status=CellStatus.EMPTY, item=None
                    )
        for shelf_idx, shelf in enumerate(self.right_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, _ in enumerate(tray.slots):
                    self.right_module[shelf_idx].trays[tray_idx].slots[slot_idx] = Cell(
                        status=CellStatus.EMPTY, item=None
                    )

    def get_all_items(self) -> List[StorageModuleCellLocation]:
        items = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, slot in enumerate(tray.slots):
                    if slot.status == CellStatus.OCCUPIED:
                        items.append(
                            StorageModuleCellLocation(
                                shelf_idx=shelf_idx,
                                tray_idx=tray_idx,
                                side=StorageModuleSide.LEFT,
                                slot_idx=slot_idx,
                            )
                        )
        for shelf_idx, shelf in enumerate(self.right_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, slot in enumerate(tray.slots):
                    if slot.status == CellStatus.OCCUPIED:
                        items.append(
                            StorageModuleCellLocation(
                                shelf_idx=shelf_idx,
                                tray_idx=tray_idx,
                                side=StorageModuleSide.RIGHT,
                                slot_idx=slot_idx,
                            )
                        )
        return items

    def get_all_free_cells(self) -> List[StorageModuleCellLocation]:
        locations: List[StorageModuleCellLocation] = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, slot in enumerate(tray.slots):
                    if slot.status == CellStatus.EMPTY:
                        locations.append(
                            StorageModuleCellLocation(
                                    shelf_idx=shelf_idx,
                                    tray_idx=tray_idx,
                                    side=StorageModuleSide.LEFT,
                                    slot_idx=slot_idx,
                            )
                        )
        for shelf_idx, shelf in enumerate(self.right_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, slot in enumerate(tray.slots):
                    if slot.status == CellStatus.EMPTY:
                        locations.append(
                            StorageModuleCellLocation(
                                    shelf_idx=shelf_idx,
                                    tray_idx=tray_idx,
                                    side=StorageModuleSide.RIGHT,
                                    slot_idx=slot_idx,
                            )
                        )
        return locations


    def get_all_cell_data(self) -> List[CellLocationData]:
        items: List[CellLocationData] = []
        for shelf_idx, shelf in enumerate(self.left_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, slot in enumerate(tray.slots):
                    if slot.status == CellStatus.OCCUPIED and slot.item:
                        items.append(
                            CellLocationData(
                                cell=slot.item,
                                location=StorageModuleCellLocation(
                                    shelf_idx=shelf_idx,
                                    tray_idx=tray_idx,
                                    side=StorageModuleSide.LEFT,
                                    slot_idx=slot_idx,
                                ),
                            )
                        )
        for shelf_idx, shelf in enumerate(self.right_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, slot in enumerate(tray.slots):
                    if slot.status == CellStatus.OCCUPIED and slot.item:
                        items.append(
                            CellLocationData(
                                cell=slot.item,
                                location=StorageModuleCellLocation(
                                    shelf_idx=shelf_idx,
                                    tray_idx=tray_idx,
                                    side=StorageModuleSide.RIGHT,
                                    slot_idx=slot_idx,
                                ),
                            )
                        )
        return items

    def get_all_items_gs1(self) -> List[CellItem]:
        items: List[CellItem] = []
        for shelf in self.left_module:
            for tray in shelf.trays:
                for slot in tray.slots:
                    if slot.status == CellStatus.OCCUPIED and slot.item:
                        items.append(slot.item)
        for shelf in self.right_module:
            for tray in shelf.trays:
                for slot in tray.slots:
                    if slot.status == CellStatus.OCCUPIED and slot.item:
                        items.append(slot.item)
        return items

    def get_item_location(self, data: GS1Data) -> StorageModuleCellLocation:
        for shelf_idx, shelf in enumerate(self.left_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, cell in enumerate(tray.slots):
                    if cell.status == CellStatus.OCCUPIED:
                        if cell.item and cell.item.gs1_data == data:
                            return StorageModuleCellLocation(
                                shelf_idx=shelf_idx,
                                tray_idx=tray_idx,
                                side=StorageModuleSide.LEFT,
                                slot_idx=slot_idx,
                            )
        for shelf_idx, shelf in enumerate(self.right_module):
            for tray_idx, tray in enumerate(shelf.trays):
                for slot_idx, cell in enumerate(tray.slots):
                    if cell.status == CellStatus.OCCUPIED:
                        if cell.item and cell.item.gs1_data == data:
                            return StorageModuleCellLocation(
                                shelf_idx=shelf_idx,
                                tray_idx=tray_idx,
                                side=StorageModuleSide.RIGHT,
                                slot_idx=slot_idx,
                            )
        raise Exception("No Item Found")
