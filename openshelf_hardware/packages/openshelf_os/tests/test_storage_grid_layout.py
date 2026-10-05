
from openshelf_os.storage import (
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
	StorageModuleSide,
)


def _cell(cell_idx: int, width: float = 50.0, height: float = 80.0, length: float = 400.0) -> Cell:
	return Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=None,
		dimensions=Dimensions(width=width, height=height, length=length),
	)


def _shelf(shelf_idx: int, cells: list[Cell]) -> Shelf:
	return Shelf(
		shelf_idx=shelf_idx,
		cells=cells,
		divider_width=8.0,
	)


def _grid(tmp_path) -> StorageGrid:
	grid = StorageGrid(
		front_module=[_shelf(0, [_cell(0), _cell(1)])],
		back_module=[],
	)
	grid.filename = str(tmp_path / "storage.json")
	return grid


def test_add_shelf_appends_next_index_with_one_cell(tmp_path) -> None:
	grid = _grid(tmp_path)

	new_shelf = grid.add_shelf(StorageModuleSide.FRONT)

	assert [shelf.shelf_idx for shelf in grid.front_module] == [0, 1]
	assert new_shelf.shelf_idx == 1
	# geometry is cloned from the side's most recent shelf
	assert new_shelf.divider_width == 8.0
	# the single new cell starts at index 0 and clones the side's cell dimensions
	assert len(new_shelf.cells) == 1
	cell = new_shelf.cells[0]
	assert cell.cell_idx == 0
	assert cell.dimensions == _cell(0).dimensions
	assert cell.status == CellStatus.AVAILABLE
	assert cell.item is None
	assert cell.calibration_point is None
	# the change is persisted
	reloaded = StorageGrid.load_from_file(grid.filename)
	assert [shelf.shelf_idx for shelf in reloaded.front_module] == [0, 1]
	assert [cell.cell_idx for cell in reloaded.front_module[1].cells] == [0]


def test_add_shelf_keeps_cell_dimensions_from_template(tmp_path) -> None:
	grid = StorageGrid(
		front_module=[],
		back_module=[
			Shelf(
				shelf_idx=0,
				cells=[_cell(0, height=150.0)],
				divider_width=8.0,
			),
			Shelf(
				shelf_idx=1,
				cells=[],
				divider_width=8.0,
			),
		],
	)
	grid.filename = str(tmp_path / "storage.json")

	new_shelf = grid.add_shelf(StorageModuleSide.FRONT)

	# shelf geometry comes from the empty top shelf, the cell from the one below
	assert new_shelf.cells[0].dimensions.height == 150.0


def test_remove_shelf_shifts_higher_shelves_down(tmp_path) -> None:
	# distinct cell widths mark the physical shelves: 10 (F:0), 20 (F:1), 30 (F:2)
	grid = StorageGrid(
		front_module=[
			_shelf(0, [_cell(0, width=10.0)]),
			_shelf(1, [_cell(0, width=20.0)]),
			_shelf(2, [_cell(0, width=30.0)]),
		],
		back_module=[],
	)
	grid.filename = str(tmp_path / "storage.json")

	grid.remove_shelf(StorageModuleSide.FRONT, 0)

	# the deleted shelf was 0, so physical 20 became index 0 and physical 30 index 1
	assert [shelf.cells[0].dimensions.width for shelf in grid.front_module] == [20.0, 30.0]

	grid.remove_shelf(StorageModuleSide.FRONT, 1)

	assert [shelf.shelf_idx for shelf in grid.front_module] == [0]
	assert grid.front_module[0].cells[0].dimensions.width == 20.0


def test_remove_shelf_keeps_cells_and_reindexes_persisted(tmp_path) -> None:
	grid = StorageGrid(
		front_module=[
			_shelf(0, [_cell(0), _cell(1)]),
			_shelf(1, [_cell(0)]),
		],
		back_module=[],
	)
	grid.filename = str(tmp_path / "storage.json")

	grid.remove_shelf(StorageModuleSide.FRONT, 0)

	assert [shelf.shelf_idx for shelf in grid.front_module] == [0]
	# the survivor keeps its cells intact under its new index
	assert [cell.cell_idx for cell in grid.front_module[0].cells] == [0]


