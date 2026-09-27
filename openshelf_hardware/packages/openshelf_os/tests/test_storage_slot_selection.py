from openshelf_os.storage import Cell, CellItem, CellStatus, Dimensions, Shelf, StorageGrid, StorageModuleSide


def build_cell(cell_idx: int, *, width: float, length: float, height: float) -> Cell:
	return Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=None,
		dimensions=Dimensions(width=width, length=length, height=height),
	)


def test_find_best_slot_prefers_small_footprint_over_shorter_medium_tray() -> None:
	grid = StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_width=400.0,
				cells=[
					build_cell(0, width=115.0, length=145.0, height=135.0),
					build_cell(1, width=85.0, length=145.0, height=185.0),
				],
			)
		],
		right_module=[],
	)
	item = CellItem(dimensions=Dimensions(width=85.0, length=145.0, height=135.0))

	location = grid.find_best_slot_for_item(item)

	assert location is not None
	assert location.side == StorageModuleSide.LEFT
	assert location.shelf_idx == 0
	assert location.cell_idx == 1


def test_find_best_slot_prefers_shortest_height_within_same_footprint() -> None:
	grid = StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_width=400.0,
				cells=[
					build_cell(0, width=85.0, length=145.0, height=235.0),
					build_cell(1, width=85.0, length=145.0, height=185.0),
				],
			)
		],
		right_module=[],
	)
	item = CellItem(dimensions=Dimensions(width=85.0, length=145.0, height=135.0))

	location = grid.find_best_slot_for_item(item)

	assert location is not None
	assert location.side == StorageModuleSide.LEFT
	assert location.shelf_idx == 0
	assert location.cell_idx == 1


def test_find_best_slot_prefers_smaller_footprint_area_over_narrower_width() -> None:
	grid = StorageGrid(
		left_module=[],
		right_module=[
			Shelf(
				shelf_idx=0,
				shelf_width=400.0,
				cells=[
					build_cell(0, width=90.0, length=200.0, height=200.0),
					build_cell(1, width=100.0, length=90.0, height=50.0),
				],
			)
		],
	)
	item = CellItem(dimensions=Dimensions(width=90.0, length=80.0, height=50.0))

	location = grid.find_best_slot_for_item(item)

	assert location is not None
	assert location.side == StorageModuleSide.RIGHT
	assert location.shelf_idx == 0
	assert location.cell_idx == 1


def test_find_best_slot_excludes_server_reserved_home_even_when_available() -> None:
	grid = StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_width=400.0,
				cells=[
					build_cell(0, width=85.0, length=145.0, height=135.0),
					build_cell(1, width=90.0, length=150.0, height=140.0),
				],
			)
		],
		right_module=[],
	)
	item = CellItem(dimensions=Dimensions(width=85.0, length=145.0, height=135.0))

	location = grid.find_best_slot_for_item(
		item,
		excluded_locations={("left_module", 0, 0)},
	)

	assert location is not None
	assert location.cell_idx == 1
