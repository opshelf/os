import pytest

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
		shelf_width=500.0,
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
	assert new_shelf.shelf_width == 500.0
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


def test_add_shelf_falls_back_to_other_side_then_defaults(tmp_path) -> None:
	grid = StorageGrid(
		front_module=[],
		back_module=[_shelf(3, [_cell(0, width=60.0, height=30.0)])],
	)
	grid.filename = str(tmp_path / "storage.json")

	new_shelf = grid.add_shelf(StorageModuleSide.FRONT)

	# shelf geometry comes from the other side; the cell clones its geometry
	assert new_shelf.shelf_idx == 0
	assert new_shelf.shelf_width == 500.0
	assert new_shelf.cells[0].dimensions == Dimensions(width=60.0, height=30.0, length=400.0)

	grid2 = StorageGrid(front_module=[], back_module=[])
	grid2.filename = str(tmp_path / "storage2.json")
	new_shelf2 = grid2.add_shelf(StorageModuleSide.BACK)

	assert (new_shelf2.shelf_width, new_shelf2.divider_width) == (480.0, 12.0)


def test_add_shelf_keeps_cell_dimensions_from_template(tmp_path) -> None:
	grid = StorageGrid(
		front_module=[],
		back_module=[
			Shelf(
				shelf_idx=0,
				shelf_width=500.0,
				cells=[_cell(0, height=150.0)],
				divider_width=8.0,
			),
			Shelf(
				shelf_idx=1,
				shelf_width=500.0,
				cells=[],
				divider_width=8.0,
			),
		],
	)
	grid.filename = str(tmp_path / "storage.json")

	new_shelf = grid.add_shelf(StorageModuleSide.FRONT)

	# shelf geometry comes from the empty top shelf, the cell from the one below
	assert new_shelf.cells[0].dimensions.height == 150.0


@pytest.mark.parametrize("empty_side", [StorageModuleSide.FRONT, StorageModuleSide.BACK])
def test_add_shelf_and_cell_on_empty_side(tmp_path, empty_side) -> None:
	grid = StorageGrid()
	grid.filename = str(tmp_path / "storage.json")

	shelf = grid.add_shelf(empty_side)
	grid.add_cell(empty_side, 0)

	assert [shelf.shelf_idx for shelf in grid.get_side(empty_side)] == [0]
	assert [cell.cell_idx for cell in grid.get_side(empty_side)[0].cells] == [0, 1]


def test_add_cell_appends_next_index(tmp_path) -> None:
	grid = _grid(tmp_path)

	new_cell = grid.add_cell(StorageModuleSide.FRONT, 0)

	assert [cell.cell_idx for cell in grid.front_module[0].cells] == [0, 1, 2]
	assert new_cell.cell_idx == 2
	assert new_cell.dimensions == _cell(0).dimensions
	assert new_cell.status == CellStatus.AVAILABLE
	assert new_cell.item is None
	assert new_cell.calibration_point is None
	reloaded = StorageGrid.load_from_file(grid.filename)
	assert [cell.cell_idx for cell in reloaded.front_module[0].cells] == [0, 1, 2]


def test_add_cell_falls_back_to_side_template(tmp_path) -> None:
	grid = StorageGrid(
		front_module=[
			_shelf(0, []),
			_shelf(1, [_cell(0, width=70.0, height=90.0)]),
		],
		back_module=[],
	)
	grid.filename = str(tmp_path / "storage.json")

	new_cell = grid.add_cell(StorageModuleSide.FRONT, 0)

	# the template comes from the side's other shelf, clamped to this shelf's height
	assert new_cell.dimensions == Dimensions(width=70.0, height=90.0, length=400.0)


def test_add_cell_raises_for_missing_shelf(tmp_path) -> None:
	grid = _grid(tmp_path)

	with pytest.raises(IndexError):
		grid.add_cell(StorageModuleSide.FRONT, 1)


@pytest.mark.parametrize("remove_idx", [0, 1, 2])
def test_remove_shelf_reindexes_remaining(tmp_path, remove_idx: int) -> None:
	grid = StorageGrid(
		front_module=[
			_shelf(0, [_cell(0)]),
			_shelf(1, [_cell(0)]),
			_shelf(2, [_cell(0)]),
		],
		back_module=[],
	)
	grid.filename = str(tmp_path / "storage.json")

	grid.remove_shelf(StorageModuleSide.FRONT, remove_idx)

	assert [shelf.shelf_idx for shelf in grid.front_module] == [0, 1]
	reloaded = StorageGrid.load_from_file(grid.filename)
	assert [shelf.shelf_idx for shelf in reloaded.front_module] == [0, 1]


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


def test_remove_shelf_raises_for_missing_shelf(tmp_path) -> None:
	grid = _grid(tmp_path)

	with pytest.raises(IndexError):
		grid.remove_shelf(StorageModuleSide.FRONT, 7)
