import json
import os

from dataclasses import dataclass, replace as dataclass_replace
from enum import Enum
from typing import Dict, Iterable, List, Mapping, Optional, Self, Tuple, Union
from pydantic import BaseModel, Field, ValidationError, model_validator
from openshelf_os.common import AxisCoordinate, write_json_file_with_backup
from openshelf_os.runtime_state import infer_mode_from_path

type Json = Dict[str, Union[str, int, float, Mapping, Iterable]]


class StorageValidationError(Exception):
	pass


DEFAULT_CELL_HEIGHT_MM = 200.0
DEFAULT_SHELF_WIDTH_MM = 480.0
DEFAULT_DIVIDER_WIDTH_MM = 12.0
DEFAULT_CELL_LENGTH_MM = 400.0

# One-time on-disk migration of the storage files written before the front/back
# cutover, which keyed their shelves under left_module/right_module.
SIDE_RENAME_BACKUP_SUFFIX = ".side-rename.bak"
_SIDE_RENAMES = (("left_module", "front_module"), ("right_module", "back_module"))


class StorageModuleSide(str, Enum):
	FRONT = "front_module"
	BACK = "back_module"


@dataclass
class CalibrationCoordinate(AxisCoordinate):
	# The z offset is only comparable when the stage was extended the way the z model is pinned,
	# so the capture conditions travel with the datum instead of being remembered by whoever
	# saved it. Absent on coordinates written before this was recorded.
	z_captured_at_stage_mm: Optional[float] = None

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
	barcode: Optional[str] = None

	# Grid files written before the upca -> barcode rename stored the item code under
	# "upca"; map it back at read time so a device upgrading from the previous
	# release keeps its barcodes. Read-time normalization only: saves always write
	# "barcode", the model itself never writes the legacy key back.
	@model_validator(mode="before")
	@classmethod
	def migrate_legacy_upca(cls, data: object) -> object:
		if isinstance(data, dict) and "upca" in data and "barcode" not in data:
			data = {**data, "barcode": data["upca"]}

		return data


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
	shelf_width: float
	cells: List[Cell]
	divider_width: float = 8.0

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
	front_module: List[Shelf] = Field(default_factory=list)
	back_module: List[Shelf] = Field(default_factory=list)
	filename: str = ""

	@staticmethod
	def _normalize_layout_indices(data: object) -> object:
		if isinstance(data, dict) is False:
			return data

		normalized = dict(data)

		for side_key in ("front_module", "back_module"):
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
		self.front_module.sort(key=lambda shelf: shelf.shelf_idx)
		self.back_module.sort(key=lambda shelf: shelf.shelf_idx)

		return self

	@classmethod
	def _migrate_side_rename(cls, target_path: str, raw_content: str, data: object) -> object:
		"""Rename the pre-cutover side keys in a storage file, in place and idempotently.

		Files written before the front/back cutover keep their shelves under the
		left_module/right_module keys. Rename them in the loaded data and rewrite the
		file with an exclusive-created backup of the original bytes, so a device
		picks the new keys up on its next load and the old file is recoverable.
		Files that already carry the new keys are left untouched.
		"""
		if isinstance(data, dict) is False:
			return data
		if "front_module" in data or "back_module" in data:
			return data
		if all(key not in data for key, _ in _SIDE_RENAMES):
			return data

		migrated = dict(data)
		for old, new in _SIDE_RENAMES:
			if old in migrated:
				migrated[new] = migrated.pop(old)

		cls._write_with_side_rename_backup(target_path, raw_content, migrated)
		return migrated

	@classmethod
	def _write_with_side_rename_backup(cls, target_path: str, raw_content: str, data: Mapping) -> None:
		backup_path = target_path + SIDE_RENAME_BACKUP_SUFFIX

		try:
			# Exclusive creation prevents a later invocation from overwriting the rollback copy.
			with open(backup_path, "xb") as stream:
				stream.write(raw_content.encode("utf-8"))
				stream.flush()
				os.fsync(stream.fileno())
		except FileExistsError:
			# The backup is left over from an interrupted migration: trust it only when it
			# still holds the bytes this load read, otherwise the file changed on disk.
			with open(backup_path, "rb") as stream:
				if stream.read() != raw_content.encode("utf-8"):
					raise StorageValidationError(
						f"{target_path}: {SIDE_RENAME_BACKUP_SUFFIX} does not match the current file; reconcile before migrating"
					)

		write_json_file_with_backup(
			target_path,
			data,
			backup_group_name="storage",
			backup_mode=infer_mode_from_path(target_path).value,
		)

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

		data = cls._migrate_side_rename(target_path, raw_content, data)

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
		if side == StorageModuleSide.BACK:
			return self.back_module
		return self.front_module

	def _set_side(self, side: StorageModuleSide, shelves: List[Shelf]) -> None:
		if side == StorageModuleSide.BACK:
			self.back_module = shelves
		else:
			self.front_module = shelves

	def remove_shelf(self, side: StorageModuleSide, shelf_idx: int) -> None:
		self.get_shelf(side, shelf_idx)
		side_list = self.get_side(side)
		remaining = sorted(
			(shelf for shelf in side_list if shelf.shelf_idx != shelf_idx),
			key=lambda shelf: shelf.shelf_idx
		)
		# Reindex so the side stays contiguous: deleting shelf 0 makes shelf 1 the new 0.
		for new_idx, shelf in enumerate(remaining):
			shelf.shelf_idx = new_idx
		self._set_side(side, remaining)
		self.save()

	def remove_cell(self, side: StorageModuleSide, shelf_idx: int, cell_idx: int) -> None:
		shelf = self.get_shelf(side, shelf_idx)
		before = len(shelf.cells)
		shelf.cells = [c for c in shelf.cells if c.cell_idx != cell_idx]

		if len(shelf.cells) == before:
			raise IndexError(f"Invalid cell_idx: {cell_idx}")

		self.save()

	def add_shelf(self, side: StorageModuleSide) -> Shelf:
		shelves = self.get_side(side)
		shelf_idx = max((shelf.shelf_idx for shelf in shelves), default=-1) + 1

		template = self._get_shelf_template(side)
		new_shelf = Shelf(
			shelf_idx=shelf_idx,
			shelf_width=template.shelf_width,
			cells=[],
			divider_width=template.divider_width,
		)
		new_shelf.cells.append(self._build_new_cell(side, new_shelf))
		shelves.append(new_shelf)
		self.save()

		return new_shelf

	def add_cell(self, side: StorageModuleSide, shelf_idx: int) -> Cell:
		shelf = self.get_shelf(side, shelf_idx)
		new_cell = self._build_new_cell(side, shelf)
		shelf.cells.append(new_cell)
		self.save()

		return new_cell

	def _get_shelf_template(self, side: StorageModuleSide) -> Shelf:
		shelves = self.get_side(side)

		if len(shelves) > 0:
			return shelves[-1]

		other_side = StorageModuleSide.BACK if side == StorageModuleSide.FRONT else StorageModuleSide.FRONT
		other_shelves = self.get_side(other_side)

		if len(other_shelves) > 0:
			return other_shelves[-1]

		return Shelf(
			shelf_idx=0,
			shelf_width=DEFAULT_SHELF_WIDTH_MM,
			cells=[],
			divider_width=DEFAULT_DIVIDER_WIDTH_MM,
		)

	def _get_cell_template(self, side: StorageModuleSide, shelf: Shelf) -> Cell:
		if len(shelf.cells) > 0:
			return shelf.cells[-1]

		for candidate_shelf in reversed(self.get_side(side)):
			if len(candidate_shelf.cells) > 0:
				return candidate_shelf.cells[-1]

		other_side = StorageModuleSide.BACK if side == StorageModuleSide.FRONT else StorageModuleSide.FRONT

		for candidate_shelf in reversed(self.get_side(other_side)):
			if len(candidate_shelf.cells) > 0:
				return candidate_shelf.cells[-1]

		return Cell(
			cell_idx=0,
			status=CellStatus.AVAILABLE,
			item=None,
			calibration_point=None,
			dimensions=Dimensions(
				width=max(float(shelf.shelf_width), 1.0),
				height=DEFAULT_CELL_HEIGHT_MM,
				length=DEFAULT_CELL_LENGTH_MM,
			),
		)

	def _build_new_cell(self, side: StorageModuleSide, shelf: Shelf, cell_idx: Optional[int] = None) -> Cell:
		if cell_idx is None:
			cell_idx = max((cell.cell_idx for cell in shelf.cells), default=-1) + 1

		template = self._get_cell_template(side, shelf)
		dimensions = template.dimensions.model_copy(deep=True)

		return Cell(
			cell_idx=cell_idx,
			status=CellStatus.AVAILABLE,
			item=None,
			calibration_point=None,
			dimensions=dimensions,
		)

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

	def get_cell_if_present(self, location: StorageModuleCellLocation) -> Optional[Cell]:
		try:
			return self.get_cell(location)
		except IndexError:
			return None

	@staticmethod
	def cell_fits_item(cell: Cell, item: CellItem) -> bool:
		return (
			cell.dimensions.width >= item.dimensions.width
			and cell.dimensions.length >= item.dimensions.length
			and cell.dimensions.height >= item.dimensions.height
		)

	def store_item(self, location: StorageModuleCellLocation, item: Optional[CellItem]) -> None:
		cell = self.get_cell(location)

		if cell.status not in (CellStatus.DISABLED, CellStatus.MAINTENANCE):
			cell.status = CellStatus.OCCUPIED

		cell.item = item

	def clear_item(self, location: StorageModuleCellLocation) -> None:
		self.get_cell(location).clear()

	def clear_all_items(self) -> None:
		for shelf in self.front_module:
			for cell in shelf.cells:
				cell.clear()

		for shelf in self.back_module:
			for cell in shelf.cells:
				cell.clear()

	def get_all_items(self) -> List[StorageModuleCellLocation]:
		items: List[StorageModuleCellLocation] = []

		for shelf in self.front_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						StorageModuleCellLocation(
							shelf_idx=shelf.shelf_idx,
							cell_idx=cell.cell_idx,
							side=StorageModuleSide.FRONT,
						)
					)

		for shelf in self.back_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						StorageModuleCellLocation(
							shelf_idx=shelf.shelf_idx,
							cell_idx=cell.cell_idx,
							side=StorageModuleSide.BACK,
						)
					)

		return items



	def get_all_cell_data(self) -> List[CellLocationData]:
		items: List[CellLocationData] = []

		for shelf in self.front_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						CellLocationData(
							cell=cell.item,
							location=StorageModuleCellLocation(
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
								side=StorageModuleSide.FRONT,
							),
						)
					)

		for shelf in self.back_module:
			for cell in shelf.cells:
				if cell.status == CellStatus.OCCUPIED:
					items.append(
						CellLocationData(
							cell=cell.item,
							location=StorageModuleCellLocation(
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
								side=StorageModuleSide.BACK,
							),
						)
					)

		return items

	def find_best_slot_for_item(
		self,
		item: CellItem,
		excluded_locations: Optional[set[tuple[str, int, int]]] = None,
	) -> Optional[StorageModuleCellLocation]:
		possible_slots: List[Tuple[StorageModuleCellLocation, Tuple[float, float, float, float, float]]] = []
		excluded_locations = excluded_locations or set()

		for shelf in self.front_module:
			for cell in shelf.cells:
				if (StorageModuleSide.FRONT.value, shelf.shelf_idx, cell.cell_idx) in excluded_locations:
					continue
				if cell.status == CellStatus.AVAILABLE and self.cell_fits_item(cell, item):
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
								side=StorageModuleSide.FRONT,
								shelf_idx=shelf.shelf_idx,
								cell_idx=cell.cell_idx,
							),
							fit_score,
						)
					)

		for shelf in self.back_module:
			for cell in shelf.cells:
				if (StorageModuleSide.BACK.value, shelf.shelf_idx, cell.cell_idx) in excluded_locations:
					continue
				if cell.status == CellStatus.AVAILABLE and self.cell_fits_item(cell, item):
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
								side=StorageModuleSide.BACK,
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
