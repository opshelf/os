"""Layout editor grid endpoints (POST /api/storage/grid/shelf, /api/storage/grid/cell)."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

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
		front_module=[
			Shelf(
				shelf_idx=0,
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
		back_module=[],
		filename=str(tmp_path / "storage-grid.json"),
	)


@pytest.mark.anyio
async def test_update_grid_cells_batch_is_atomic_on_missing_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.put(
			"/api/storage/grid/cells",
			params={"side": "front_module"},
			json={
				"cells": [
					{"shelf_idx": 0, "cell_idx": 0, "status": "maintenance"},
					{"shelf_idx": 0, "cell_idx": 9, "status": "disabled"},  # missing
				]
			},
		)

	assert response.status_code == 404
	# the valid cell was NOT mutated

	c0 = grid.get_cell(StorageModuleCellLocation(side=StorageModuleSide.FRONT, shelf_idx=0, cell_idx=0))
	assert c0.status is CellStatus.AVAILABLE


@pytest.mark.anyio
async def test_delete_grid_shelf_refuses_stored_item_at_or_above(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)
	grid.add_shelf(StorageModuleSide.FRONT)

	async with AsyncSessionLocal() as session:
		item = Item(
			barcode="123456789012",
			name="Stored",
			dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
		)
		session.add(item)
		await session.flush()
		session.add(
			ItemLocation(item_id=item.id, side="front_module", shelf_idx=1, cell_idx=0)
		)
		await session.commit()

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/shelf", params={"side": "front_module", "shelf_idx": 0}
		)

	assert response.status_code == 409, response.text
	# the shelf was not removed
	assert [shelf.shelf_idx for shelf in grid.front_module] == [0, 1]


@pytest.mark.anyio
async def test_delete_grid_shelf_refuses_tray_home_at_or_above(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)
	grid.add_shelf(StorageModuleSide.FRONT)

	async with AsyncSessionLocal() as session:
		session.add(
			StorageBin(
				barcode="999012345678",
				side="front_module",
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
			"/api/storage/grid/shelf", params={"side": "front_module", "shelf_idx": 0}
		)

	assert response.status_code == 409, response.text
	# the shelf was not removed
	assert [shelf.shelf_idx for shelf in grid.front_module] == [0, 1]

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
			ItemLocation(item_id=item.id, side="front_module", shelf_idx=0, cell_idx=0)
		)
		await session.commit()

	with patch("openshelf_os_server.core.robot.robot_worker", build_worker(grid)):
		response = await client.delete(
			"/api/storage/grid/cell",
			params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
		)

	assert response.status_code == 409, response.text
	# the cell was not removed
	assert [cell.cell_idx for cell in grid.front_module[0].cells] == [0, 1]


@pytest.mark.anyio
async def test_delete_grid_cell_refuses_tray_home_at_exact_cell(client: AsyncClient, tmp_path) -> None:
	grid = build_grid(tmp_path)

	async with AsyncSessionLocal() as session:
		session.add(
			StorageBin(
				barcode="999012345678",
				side="front_module",
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
			params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
		)

	assert response.status_code == 409, response.text
	# the cell was not removed
	assert [cell.cell_idx for cell in grid.front_module[0].cells] == [0, 1]
