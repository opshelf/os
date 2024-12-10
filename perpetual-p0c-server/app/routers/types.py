from pydantic import BaseModel
from enum import Enum
from typing import Optional, List, Tuple

class Coordinate(BaseModel):
    x: float
    y: float
    z: float

    def __add__(self, other: "Coordinate") -> "Coordinate":
        return Coordinate(x=self.x + other.x, y=self.y + other.y, z=self.z + other.z)

    def __sub__(self, other: "Coordinate") -> "Coordinate":
        return Coordinate(x=self.x - other.x, y=self.y - other.y, z=self.z - other.z)

class ItemType(str, Enum):
    OPTIONAL = "OPTIONAL"
    BOTTLE = "BOTTLE"
    BOX = "BOTTLE"


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
    item_type: ItemType
    gs1_data: Optional[GS1Data]
    weight: Optional[float]
    height: Optional[float]
    width: Optional[float]


class Cell(BaseModel):
    status: CellStatus
    item: Optional[CellItem]


"""
class Tray(BaseModel):
    cells: List[Cell]
    tray_length: float
    tray_width: float
    tray_id: Optional[str]
    side_wall_thickness: float = 3.0
    rear_wall_thickness: float = 3.0
    divider_thickness: float = 5.0

    @property
    def slots(self) -> int:
        return len(self.cells)

    @property
    def slot_depth(self) -> float:
        return self.tray_width - self.rear_wall_thickness

    @property
    def exterior_slot_width(self) -> float:
        return (self.interior_width + (self.divider_thickness * (self.slots - 1))) / self.slots

    @property
    def interior_slot_width(self) -> float:
        return self.exterior_slot_width - self.divider_thickness * 2

    @property
    def interior_width(self) -> float:
        return self.tray_length - self.side_wall_thickness * 2

    @property
    def available_width(self) -> float:
        return self.tray_length - self.side_wall_thickness * 2 - self.divider_thickness * 2

    def slot_center(self, idx: int) -> float:
        return (
            self.side_wall_thickness
            + self.divider_thickness
            + self.interior_slot_width / 2
            + self.divider_thickness * idx
            + self.interior_slot_width * idx
        )

    def exterior_slot_edges(self, idx: int) -> Tuple[float, float]:
        left_edge = self.side_wall_thickness + self.exterior_slot_width * (idx + 1)
        right_edge = self.side_wall_thickness + self.exterior_slot_width * idx
        return (left_edge, right_edge)

    def interior_slot_edges(self, idx: int) -> Tuple[float, float]:
        left_edge = (
            self.side_wall_thickness
            + self.divider_thickness
            + self.interior_slot_width * idx
            + self.divider_thickness * idx
        )
        right_edge = (
            self.side_wall_thickness
            + self.divider_thickness
            + idx * self.interior_slot_width
            + idx * self.divider_thickness
        )
        return (left_edge, right_edge)


class TrayLocation(BaseModel):
    tray: Tray
    column: int


class Shelf(BaseModel):
    trays: List[TrayLocation]
    left_cal_point: Coordinate
    right_cal_point: Coordinate
    columns: int
    shelf_height: float

    def __init__(self, **data) -> None:
        super().__init__(**data)
        self.trays.sort(key=lambda x: x.column)

    def get_tray_column(self, idx: int) -> Optional[int]:
        if idx >= len(self.trays):
            return None
        return self.trays[idx].column

    def get_column_coordinate(self, idx: int) -> Optional[Coordinate]:
        if idx >= self.columns:
            return None
        difference = self.right_cal_point - self.left_cal_point
        return Coordinate(
            x=self.left_cal_point.x + (difference.x * (idx / (self.columns - 1))),
            y=self.left_cal_point.y + (difference.y * (idx / (self.columns - 1))),
            z=self.left_cal_point.z + (difference.z * (idx / (self.columns - 1))),
        )
"""

class Trays(BaseModel):
    width: float
    height: float
    calibration_point: Optional[Coordinate]
    empty: bool = True

class Shelf(BaseModel):
    shelf_height: float
    shelf_width: float
    trays: List[Trays]

class StorageGrid(BaseModel):
    left_module: List[Shelf]
    right_module: List[Shelf]
