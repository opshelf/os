import json
import os

from dataclasses import replace as dataclass_replace
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Self, Tuple, Union
from pydantic import BaseModel, Field, ValidationError, model_validator
from openshelf_os.common import AxisCoordinate, write_json_file_with_backup
from openshelf_os.runtime_state import infer_mode_from_path

type Json = Dict[str, Union[str, int, float, Mapping, Iterable]]


class StorageValidationError(Exception):
	pass


class StorageModuleSide(str, Enum):
	RIGHT = "right_module"
	LEFT = "left_module"


class CalibrationCoordinate(AxisCoordinate):
	def __add__(self, rhs: "CalibrationCoordinate") -> "CalibrationCoordinate":  # type: ignore[override]
		self._assert_same_side(rhs, "add")

		return CalibrationCoordinate(
			x=self.x + rhs.x,
			y=self.y + rhs.y,
			z_offset=self.z_offset + rhs.z_offset,
			s=self.s,
		)

	def __sub__(self, rhs: "CalibrationCoordinate") -> "CalibrationCoordinate":  # type: ignore[override]
		self._assert_same_side(rhs, "subtract")

		return CalibrationCoordinate(
			x=self.x - rhs.x,
			y=self.y - rhs.y,
			z_offset=self.z_offset - rhs.z_offset,
			s=self.s,
		)

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
	cell_idx: int = Field(default=0, ge=0)
	status: CellStatus
	item: Optional[CellItem]
	calibration_point: Optional[CalibrationCoordinate]
	dimensions: Dimensions
	is_manual: bool = False

	def clear(self) -> None:
		if self.status == CellStatus.OCCUPIED:
			self.status = CellStatus.AVAILABLE
			self.item = None


class StorageModuleCellLocation(BaseModel):
	side: StorageModuleSide
	shelf_idx: int
	cell_idx: int


class Shelf(BaseModel):
	shelf_idx: int = Field(default=0, ge=0)
	shelf_height: float
	shelf_width: float
	cells: List[Cell]
	divider_width: float = 8.0

	@model_validator(mode="after")
	def verify_tray_heights(self) -> Self:
		if all(self.shelf_height >= cell.dimensions.height for cell in self.cells):
			return self

		raise ValidationError("All Trays must be less than the height of the containing shelf")

	@model_validator(mode="after")
	def sort_cells(self) -> Self:
		self.cells.sort(key=lambda cell: cell.cell_idx)

		return self

	def get_cell(self, cell_idx: int) -> Cell:
		for cell in self.cells:
			if cell.cell_idx == cell_idx:
				return cell

		raise IndexError(f"Invalid cell_idx: {cell_idx}")

	def get_cell_location(self, cell_idx: int) -> CalibrationCoordinate:
		cell = self.get_cell(cell_idx)

		if cell.calibration_point is None:
			raise Exception("Invalid Calibration Point")

		return dataclass_replace(cell.calibration_point)


class CellLocationData(BaseModel):
	cell: Optional[CellItem]
	location: StorageModuleCellLocation


class StorageGrid(BaseModel):
	left_module: List[Shelf] = Field(default_factory=list)
	right_module: List[Shelf] = Field(default_factory=list)
	filename: str = ""

	@staticmethod
	def _normalize_layout_indices(data: object) -> object:
		if isinstance(data, dict) is False:
			return data

		normalized = dict(data)

		for side_key in ("left_module", "right_module"):
			shelves_raw = normalized.get(side_key)

			if isinstance(shelves_raw, list) is False:
				continue

			normalized_shelves: List[dict] = []

			for shelf_position, shelf_raw in enumerate(shelves_raw):
				if isinstance(shelf_raw, Shelf):
					normalized_shelves.append(shelf_raw)
					continue

				if isinstance(shelf_raw, dict) is False:
					continue

				shelf_data = dict(shelf_raw)

				if "shelf_idx" not in shelf_data:
					shelf_data["shelf_idx"] = shelf_position

				cells_raw = shelf_data.get("cells")

				if isinstance(cells_raw, list) is True:
					normalized_cells: List[dict] = []

					for cell_position, cell_raw in enumerate(cells_raw):
						if isinstance(cell_raw, Cell):
							normalized_cells.append(cell_raw)
							continue

						if isinstance(cell_raw, dict) is False:
							continue

						cell_data = dict(cell_raw)

						if "cell_idx" not in cell_data:
							cell_data["cell_idx"] = cell_position

						normalized_cells.append(cell_data)

					shelf_data["cells"] = normalized_cells

				normalized_shelves.append(shelf_data)

			normalized[side_key] = normalized_shelves

		return normalized

	@model_validator(mode="before")
	@classmethod
	def migrate_sparse_layout(cls, data: object) -> object:
		return cls._normalize_layout_indices(data)

	@model_validator(mode="after")
	def sort_shelves(self) -> Self:
		self.left_module.sort(key=lambda shelf: shelf.shelf_idx)
		self.right_module.sort(key=lambda shelf: shelf.shelf_idx)

		return self

	@classmethod
	def load_from_file(cls, path: str) -> "StorageGrid":
		target_path = os.path.abspath(path)

		if os.path.exists(target_path) is False:
			raise StorageValidationError(f"storage file not found: {target_path}")

		with open(target_path, "r") as f:
			raw_content = f.read()

		try:
			data = json.loads(raw_content)
		except json.JSONDecodeError as e:
			raise StorageValidationError(f"invalid json in storage file: {e}") from e

		try:
			grid = cls.model_validate(data)
		except ValidationError as e:
			raise StorageValidationError(f"storage schema validation failed: {e}") from e

		grid.filename = target_path
		return grid

	def save_to_file(self, filename: str, *, version_id: str | None = None) -> None:
		target_path = os.path.abspath(filename)
		self.filename = target_path

		write_json_file_with_backup(
			target_path,
			self.model_dump(mode="json"),
			backup_group_name="storage",
			backup_mode=infer_mode_from_path(target_path).value,
			version_id=version_id,
		)

	def save(self, *, version_id: str | None = None) -> None:
		self.save_to_file(self.filename, version_id=version_id)

	def get_side(self, side: StorageModuleSide) -> List[Shelf]:
		if side == StorageModuleSide.RIGHT:
			return self.right_module
		return self.left_module

	def _set_side(self, side: StorageModuleSide, shelves: List[Shelf]) -> None:
		if side == StorageModuleSide.RIGHT:
			self.right_module = shelves
		else:
			self.left_module = shelves

	def remove_shelf(self, side: StorageModuleSide, shelf_idx: int) -> None:
		self.get_shelf(side, shelf_idx)
		side_list = self.get_side(side)
		self._set_side(side, [s for s in side_list if s.shelf_idx != shelf_idx])
		self.save()

	def remove_cell(self, side: StorageModuleSide, shelf_idx: int, cell_idx: int) -> None:
		shelf = self.get_shelf(side, shelf_idx)
		before = len(shelf.cells)
		shelf.cells = [c for c in shelf.cells if c.cell_idx != cell_idx]

		if len(shelf.cells) == before:
			raise IndexError(f"Invalid cell_idx: {cell_idx}")

		self.save()

	def get_shelf(self, side: StorageModuleSide, shelf_idx: int) -> Shelf:
		for shelf in self.get_side(side):
			if shelf.shelf_idx == shelf_idx:
				return shelf

		raise IndexError(f"Invalid shelf_idx: {shelf_idx}")

	def get_cell(self, location: StorageModuleCellLocation) -> Cell:
		shelf = self.get_shelf(location.side, location.shelf_idx)

		return shelf.get_cell(location.cell_idx)

	def get_cell_coordinate(self, location: StorageModuleCellLocation) -> CalibrationCoordinate:
		shelf = self.get_shelf(location.side, location.shelf_idx)

		return shelf.get_cell_location(location.cell_idx)

	def store_item(self, location: StorageModuleCellLocation, item: Optional[CellItem]) -> None:
		cell = self.get_cell(location)

		if cell.status not in (CellStatus.DISABLED, CellStatus.MAINTENANCE):
			cell.status = CellStatus.OCCUPIED

		cell.item = item

	def clear_item(self, location: StorageModuleCellLocation) -> None:
		self.get_cell(location).clear()

	def clear_all_items(self) -> None:
		for shelf in self.left_module:
			for cell in shelf.cells:
				cell.clear()

		for shelf in self.right_module:
			for cell in shelf.cells:
				cell.clear()

	def get_all_items(self) -> List[StorageModuleCellLocation]:
		items: List[StorageModuleCellLocation] = []

		for shelf in self.left_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						StorageModuleCellLocation(
							shelf_idx=shelf.shelf_idx,
							cell_idx=cell.cell_idx,
							side=StorageModuleSide.LEFT,
						)
					)

		for shelf in self.right_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						StorageModuleCellLocation(
							shelf_idx=shelf.shelf_idx,
							cell_idx=cell.cell_idx,
							side=StorageModuleSide.RIGHT,
						)
					)

		return items



	def get_all_cell_data(self) -> List[CellLocationData]:
		items: List[CellLocationData] = []

		for shelf in self.left_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						CellLocationData(
							cell=cell.item,
							location=StorageModuleCellLocation(
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
								side=StorageModuleSide.LEFT,
							),
						)
					)

		for shelf in self.right_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						CellLocationData(
							cell=cell.item,
							location=StorageModuleCellLocation(
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
								side=StorageModuleSide.RIGHT,
							),
						)
					)

		return items

	def find_best_slot_for_item(self, item: CellItem) -> Optional[StorageModuleCellLocation]:
		possible_slots: List[Tuple[StorageModuleCellLocation, Tuple[float, float, float, float, float]]] = []

		for shelf in self.left_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.AVAILABLE and (
					cell.dimensions.width >= item.dimensions.width
					and cell.dimensions.length >= item.dimensions.length
					and cell.dimensions.height >= item.dimensions.height
				):
					unused_volume = (
						cell.dimensions.width * cell.dimensions.length * cell.dimensions.height
						- item.dimensions.width * item.dimensions.length * item.dimensions.height
					)
					footprint_area = cell.dimensions.width * cell.dimensions.length
					fit_score = (
						footprint_area,
						cell.dimensions.height,
						cell.dimensions.width,
						cell.dimensions.length,
						unused_volume,
					)

					possible_slots.append(
						(
							StorageModuleCellLocation(
								side=StorageModuleSide.LEFT,
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
							),
							fit_score,
						)
					)

		for shelf in self.right_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.AVAILABLE and (
					cell.dimensions.width >= item.dimensions.width
					and cell.dimensions.length >= item.dimensions.length
					and cell.dimensions.height >= item.dimensions.height
				):
					unused_volume = (
						cell.dimensions.width * cell.dimensions.length * cell.dimensions.height
						- item.dimensions.width * item.dimensions.length * item.dimensions.height
					)
					footprint_area = cell.dimensions.width * cell.dimensions.length
					fit_score = (
						footprint_area,
						cell.dimensions.height,
						cell.dimensions.width,
						cell.dimensions.length,
						unused_volume,
					)

					possible_slots.append(
						(
							StorageModuleCellLocation(
								side=StorageModuleSide.RIGHT,
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
							),
							fit_score,
						)
					)

		possible_slots.sort(key=lambda entry: entry[1])

		if possible_slots:
			return possible_slots[0][0]

		return None

