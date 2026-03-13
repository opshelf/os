from perpetual_os1.storage import (
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
	StorageModuleCellLocation,
	StorageModuleSide,
)


def test_storage_grid_assigns_missing_indices_from_payload() -> None:
	grid = StorageGrid.model_validate(
		{
			"left_module": [
				{
					"shelf_height": 100.0,
					"shelf_width": 200.0,
					"cells": [
						{
							"status": "available",
							"item": None,
							"calibration_point": {"x": 10.0, "y": 20.0, "z_offset": 0.0, "s": 0},
							"dimensions": {"width": 50.0, "height": 100.0, "length": 400.0},
						}
					],
				}
			],
			"right_module": [],
		}
	)

	assert grid.left_module[0].shelf_idx == 0
	assert grid.left_module[0].cells[0].cell_idx == 0


def test_storage_grid_resolves_sparse_indices_by_explicit_ids() -> None:
	grid = StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=5,
				shelf_height=120.0,
				shelf_width=220.0,
				cells=[
					Cell(
						cell_idx=7,
						status=CellStatus.AVAILABLE,
						item=None,
						calibration_point=CalibrationCoordinate(x=100.0, y=200.0, z_offset=1.0, s=0),
						dimensions=Dimensions(width=60.0, height=90.0, length=400.0),
					)
				],
			)
		],
		right_module=[],
	)
	location = StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=5, cell_idx=7)

	cell = grid.get_cell(location)
	coordinate = grid.get_cell_coordinate(location)

	assert cell.cell_idx == 7
	assert coordinate.x == 100.0
	assert coordinate.y == 200.0
	assert coordinate.z_offset == 1.0
