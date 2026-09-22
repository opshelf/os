"""Layout editor grid endpoints (POST /api/storage/grid/shelf, /api/storage/grid/cell)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from fastapi import HTTPException

import openshelf_os_server.routers.storage as storage_router
from openshelf_os_server.database.models import Item, ItemLocation, StorageBin
from openshelf_os_server.database.session import AsyncSessionLocal

from openshelf_os.storage import (
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
	StorageModuleCellLocation,
	StorageModuleSide,
)


def build_worker(grid: StorageGrid):
	return SimpleNamespace(_robot=SimpleNamespace(_grid=grid))


def build_grid(tmp_path) -> StorageGrid:
	return StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_height=210.0,
				shelf_width=480.0,
				divider_width=12.0,
				cells=[
					Cell(
						cell_idx=0,
						status=CellStatus.AVAILABLE,
						item=None,
						calibration_point=None,
						dimensions=Dimensions(width=55.0, height=90.0, length=400.0),
					),
					Cell(
						cell_idx=1,
						status=CellStatus.AVAILABLE,
						item=None,
						calibration_point=None,
						dimensions=Dimensions(width=55.0, height=90.0, length=400.0),
					),
				],
			)
		],
		right_module=[],
		filename=str(tmp_path / "storage-grid.json"),
	)


@pytest.mark.anyio
async def test_add_grid_shelf(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.post("/api/storage/grid/shelf", params={"side": "left_module"})

	assert response.status_code == 200, response.text
	assert response.json() == {"success": True}

	# new shelf takes the next index with one new cell
	assert [shelf.shelf_idx for shelf in grid.left_module] == [0, 1]
	new_shelf = grid.left_module[1]
	assert new_shelf.shelf_idx == 1
	# geometry is cloned from the existing shelf
	assert new_shelf.shelf_height == 210.0
	assert new_shelf.shelf_width == 480.0
	assert new_shelf.divider_width == 12.0
	assert [cell.cell_idx for cell in new_shelf.cells] == [0]
	assert new_shelf.cells[0].dimensions == grid.left_module[0].cells[1].dimensions
	assert (tmp_path / "storage-grid.json").exists()


@pytest.mark.anyio
async def test_add_grid_shelf_requires_storage_write(client: AsyncClient, tmp_path, unauthenticated_client: AsyncClient) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await unauthenticated_client.post(
			"/api/storage/grid/shelf", params={"side": "left_module"}
		)

	assert response.status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize("side", ["left_module", "right_module"])
async def test_add_grid_shelf_on_empty_side(client: AsyncClient, tmp_path, side) -> None:
	grid = build_grid(tmp_path)
	if side == "left_module":
		grid.left_module = []

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.post("/api/storage/grid/shelf", params={"side": side})

	assert response.status_code == 200, response.text
	shelves = grid.get_side(side)
	assert [shelf.shelf_idx for shelf in shelves] == [0]
	assert [cell.cell_idx for cell in shelves[0].cells] == [0]


@pytest.mark.anyio
async def test_add_grid_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.post(
			"/api/storage/grid/cell", params={"side": "left_module", "shelf_idx": 0}
		)

	assert response.status_code == 200, response.text
	assert response.json() == {"success": True}
	assert [cell.cell_idx for cell in grid.left_module[0].cells] == [0, 1, 2]
	assert grid.left_module[0].cells[2].dimensions == grid.left_module[0].cells[0].dimensions
	assert (tmp_path / "storage-grid.json").exists()


@pytest.mark.anyio
async def test_add_grid_cell_reports_missing_shelf(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.post(
			"/api/storage/grid/cell", params={"side": "left_module", "shelf_idx": 7}
		)

	assert response.status_code == 404


@pytest.mark.anyio
async def test_add_grid_shelf_requires_side_param(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.post("/api/storage/grid/shelf")

	assert response.status_code == 422


@pytest.mark.anyio
async def test_delete_grid_shelf_reindexes(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)
	grid.add_shelf(StorageModuleSide.LEFT)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/shelf", params={"side": "left_module", "shelf_idx": 0}
		)

	assert response.status_code == 200, response.text
	assert response.json() == {"success": True}
	# deleting the lower shelf shifts the survivor down to index 0
	assert [shelf.shelf_idx for shelf in grid.left_module] == [0]
	assert (tmp_path / "storage-grid.json").exists()



@pytest.mark.anyio
async def test_delete_grid_shelf_reports_missing_shelf(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/shelf", params={"side": "left_module", "shelf_idx": 7}
		)

	assert response.status_code == 404

@pytest.mark.anyio
async def test_update_grid_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={
				"x": 100.0,
				"y": 200.0,
				"z_offset": 5.0,
				"status": "maintenance",
				"dimensions": {"width": 60.0, "height": 95.0, "length": 380.0},
			},
		)

	assert response.status_code == 200, response.text
	assert response.json() == {"success": True}


	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0)
	)
	assert cell.calibration_point is not None
	assert (cell.calibration_point.x, cell.calibration_point.y, cell.calibration_point.z_offset) == (
		100.0,
		200.0,
		5.0,
	)
	# s is derived from the queried side
	assert cell.calibration_point.s == 0
	assert cell.status is CellStatus.MAINTENANCE
	assert cell.dimensions == Dimensions(width=60.0, height=95.0, length=380.0)
	assert (tmp_path / "storage-grid.json").exists()


@pytest.mark.anyio
async def test_update_grid_cell_partial_status_only(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 1},
			json={"status": "disabled"},
		)

	assert response.status_code == 200, response.text


	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=1)
	)
	# omitted fields keep their values
	assert cell.status is CellStatus.DISABLED
	assert cell.calibration_point is None
	assert cell.dimensions == Dimensions(width=55.0, height=90.0, length=400.0)


@pytest.mark.anyio
async def test_update_grid_cell_partial_dimensions_only(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"dimensions": {"width": 50.0, "height": 85.0, "length": 390.0}},
		)

	assert response.status_code == 200, response.text


	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0)
	)
	assert cell.status is CellStatus.AVAILABLE
	assert cell.calibration_point is None
	assert cell.dimensions == Dimensions(width=50.0, height=85.0, length=390.0)


@pytest.mark.anyio
async def test_update_grid_cell_coordinates_on_right_side(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)
	grid.add_shelf(StorageModuleSide.RIGHT)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "right_module", "shelf_idx": 0, "cell_idx": 0},
			json={"x": 1.0, "y": 2.0, "z_offset": 3.0},
		)

	assert response.status_code == 200, response.text


	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.RIGHT, shelf_idx=0, cell_idx=0)
	)
	assert cell.calibration_point is not None
	assert cell.calibration_point.s == 1


@pytest.mark.anyio
async def test_update_grid_cell_rejects_partial_coordinates(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"x": 100.0},
		)

	assert response.status_code == 400


@pytest.mark.anyio
async def test_update_grid_cell_rejects_invalid_status(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"status": "broken"},
		)

	assert response.status_code == 400


@pytest.mark.anyio
async def test_update_grid_cell_reports_missing_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 9},
			json={"status": "maintenance"},
		)

	assert response.status_code == 404

@pytest.mark.anyio
async def test_update_grid_cell_single_is_manual(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"is_manual": True},
		)

	assert response.status_code == 200, response.text


	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0)
	)
	assert cell.is_manual is True
	# other fields untouched
	assert cell.calibration_point is None
	assert cell.dimensions == Dimensions(width=55.0, height=90.0, length=400.0)


@pytest.mark.anyio
async def test_update_grid_cells_batch(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cells",
			params={"side": "left_module"},
			json={
				"cells": [
					{"shelf_idx": 0, "cell_idx": 0, "status": "maintenance", "is_manual": True},
					{"shelf_idx": 0, "cell_idx": 1, "dimensions": {"width": 61.0, "height": 91.0, "length": 401.0}},
				]
			},
		)

	assert response.status_code == 200, response.text
	assert response.json() == {"success": True}


	c0 = grid.get_cell(StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0))
	c1 = grid.get_cell(StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=1))
	assert c0.status is CellStatus.MAINTENANCE
	assert c0.is_manual is True
	assert c1.dimensions == Dimensions(width=61.0, height=91.0, length=401.0)
	assert (tmp_path / "storage-grid.json").exists()


@pytest.mark.anyio
async def test_update_grid_cells_batch_is_atomic_on_missing_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cells",
			params={"side": "left_module"},
			json={
				"cells": [
					{"shelf_idx": 0, "cell_idx": 0, "status": "maintenance"},
					{"shelf_idx": 0, "cell_idx": 9, "status": "disabled"},  # missing
				]
			},
		)

	assert response.status_code == 404
	# the valid cell was NOT mutated

	c0 = grid.get_cell(StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0))
	assert c0.status is CellStatus.AVAILABLE


@pytest.mark.anyio
async def test_update_grid_cells_batch_is_atomic_on_invalid_status(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cells",
			params={"side": "left_module"},
			json={
				"cells": [
					{"shelf_idx": 0, "cell_idx": 0, "dimensions": {"width": 70.0, "height": 90.0, "length": 400.0}},
					{"shelf_idx": 0, "cell_idx": 1, "status": "broken"},  # invalid
				]
			},
		)

	assert response.status_code == 400
	# the valid cell was NOT mutated

	c0 = grid.get_cell(StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0))
	assert c0.dimensions == Dimensions(width=55.0, height=90.0, length=400.0)


@pytest.mark.anyio
async def test_update_grid_cells_batch_rejects_empty(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cells",
			params={"side": "left_module"},
			json={"cells": []},
		)

	assert response.status_code == 400


@pytest.mark.anyio
async def test_update_grid_cell_rejects_over_tall_dimensions(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"dimensions": {"width": 55.0, "height": 211.0, "length": 400.0}},
		)

	assert response.status_code == 400

	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0)
	)
	# the grid is unchanged
	assert cell.dimensions == Dimensions(width=55.0, height=90.0, length=400.0)


@pytest.mark.anyio
async def test_update_grid_cell_rejects_non_positive_dimensions(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"dimensions": {"width": 55.0, "height": 0.0, "length": 400.0}},
		)

	assert response.status_code == 400

	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0)
	)
	# the grid is unchanged
	assert cell.dimensions == Dimensions(width=55.0, height=90.0, length=400.0)


@pytest.mark.anyio
async def test_update_grid_cell_accepts_height_equal_to_shelf_height(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
			json={"dimensions": {"width": 55.0, "height": 210.0, "length": 400.0}},
		)

	assert response.status_code == 200, response.text

	cell = grid.get_cell(
		StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0)
	)
	# height equal to the shelf height is allowed
	assert cell.dimensions == Dimensions(width=55.0, height=210.0, length=400.0)


@pytest.mark.anyio
async def test_update_grid_cells_batch_is_atomic_on_over_tall_dimensions(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cells",
			params={"side": "left_module"},
			json={
				"cells": [
					{"shelf_idx": 0, "cell_idx": 0, "status": "maintenance"},
					{"shelf_idx": 0, "cell_idx": 1, "dimensions": {"width": 55.0, "height": 211.0, "length": 400.0}},  # over-tall
				]
			},
		)

	assert response.status_code == 400
	# the valid cell was NOT mutated

	c0 = grid.get_cell(StorageModuleCellLocation(side=StorageModuleSide.LEFT, shelf_idx=0, cell_idx=0))
	assert c0.status is CellStatus.AVAILABLE


@pytest.mark.anyio
async def test_grid_mutation_refused_while_laser_calibration_running(
	client: AsyncClient, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
	grid = build_grid(tmp_path)

	async def _blocked() -> None:
		raise HTTPException(status_code=409, detail="laser auto calibration is running")

	monkeypatch.setattr(storage_router, "ensure_laser_auto_calibration_not_running", _blocked)
	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.post("/api/storage/grid/shelf", params={"side": "left_module"})

	assert response.status_code == 409, response.text
	# the grid is unchanged
	assert [shelf.shelf_idx for shelf in grid.left_module] == [0]


@pytest.mark.anyio
async def test_delete_grid_shelf_refuses_stored_item_at_or_above(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)
	grid.add_shelf(StorageModuleSide.LEFT)

	async with AsyncSessionLocal() as session:
		item = Item(
			barcode="123456789012",
			name="Stored",
			dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
		)
		session.add(item)
		await session.flush()
		session.add(
			ItemLocation(item_id=item.id, side="left_module", shelf_idx=1, cell_idx=0)
		)
		await session.commit()

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/shelf", params={"side": "left_module", "shelf_idx": 0}
		)

	assert response.status_code == 409, response.text
	# the shelf was not removed
	assert [shelf.shelf_idx for shelf in grid.left_module] == [0, 1]


@pytest.mark.anyio
async def test_delete_grid_shelf_refuses_tray_home_at_or_above(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)
	grid.add_shelf(StorageModuleSide.LEFT)

	async with AsyncSessionLocal() as session:
		session.add(
			StorageBin(
				barcode="999012345678",
				side="left_module",
			shelf_idx=1,
			cell_idx=0,
			inner_dimensions={"width": 55.0, "height": 90.0, "length": 400.0},
			max_items=6,
			compartment_layout={"rows": 2, "columns": 3},
			status="at_home",
			)
		)
		await session.commit()

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/shelf", params={"side": "left_module", "shelf_idx": 0}
		)

	assert response.status_code == 409, response.text
	# the shelf was not removed
	assert [shelf.shelf_idx for shelf in grid.left_module] == [0, 1]

@pytest.mark.anyio
async def test_delete_grid_cell_refuses_stored_item_at_exact_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	async with AsyncSessionLocal() as session:
		item = Item(
			barcode="123456789012",
			name="Stored",
			dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
		)
		session.add(item)
		await session.flush()
		session.add(
			ItemLocation(item_id=item.id, side="left_module", shelf_idx=0, cell_idx=0)
		)
		await session.commit()

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
		)

	assert response.status_code == 409, response.text
	# the cell was not removed
	assert [cell.cell_idx for cell in grid.left_module[0].cells] == [0, 1]


@pytest.mark.anyio
async def test_delete_grid_cell_refuses_tray_home_at_exact_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	async with AsyncSessionLocal() as session:
		session.add(
			StorageBin(
				barcode="999012345678",
				side="left_module",
			shelf_idx=0,
			cell_idx=0,
			inner_dimensions={"width": 55.0, "height": 90.0, "length": 400.0},
			max_items=6,
			compartment_layout={"rows": 2, "columns": 3},
			status="at_home",
			)
		)
		await session.commit()

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
		)

	assert response.status_code == 409, response.text
	# the cell was not removed
	assert [cell.cell_idx for cell in grid.left_module[0].cells] == [0, 1]


@pytest.mark.anyio
async def test_delete_grid_cell_succeeds_when_unoccupied(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/cell",
			params={"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
		)

	assert response.status_code == 200, response.text
	assert response.json() == {"success": True}
	# the cell was removed
	assert [cell.cell_idx for cell in grid.left_module[0].cells] == [1]
