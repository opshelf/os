from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from perpetual_os1.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, Shelf, StorageGrid


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
						calibration_point=CalibrationCoordinate(x=100.0, y=200.0, z_offset=5.0, s=0),
						dimensions=Dimensions(width=55.0, height=90.0, length=400.0),
					)
				],
			)
		],
		right_module=[],
		filename=str(tmp_path / 'storage-grid.json'),
	)


def build_worker(grid: StorageGrid):
	return SimpleNamespace(_robot=SimpleNamespace(_grid=grid))


def build_empty_grid(tmp_path) -> StorageGrid:
	return StorageGrid(
		left_module=[],
		right_module=[],
		filename=str(tmp_path / 'storage-grid.json'),
	)


def build_grid_with_empty_shelf(tmp_path) -> StorageGrid:
	return StorageGrid(
		left_module=[
			Shelf(
				shelf_idx=0,
				shelf_height=210.0,
				shelf_width=480.0,
				divider_width=12.0,
				cells=[],
			)
		],
		right_module=[],
		filename=str(tmp_path / 'storage-grid.json'),
	)


@pytest.mark.anyio
async def test_save_calibration_creates_missing_cell_from_existing_shelf(client, tmp_path) -> None:
	grid = build_grid(tmp_path)
	worker = build_worker(grid)

	with patch('perpetual_os1_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={
				'side': 'left_module',
				'shelf_idx': 0,
				'cell_idx': 3,
				'x': 475.0,
				'y': 630.0,
				'z_offset': 0.0,
			},
		)

	assert response.status_code == 200

	new_cell = next(cell for cell in grid.left_module[0].cells if cell.cell_idx == 3)

	assert new_cell.status == CellStatus.AVAILABLE
	assert new_cell.item is None
	assert new_cell.dimensions == grid.left_module[0].cells[0].dimensions
	assert new_cell.calibration_point is not None
	assert new_cell.calibration_point.x == pytest.approx(475.0)
	assert new_cell.calibration_point.y == pytest.approx(630.0)
	assert new_cell.calibration_point.z_offset == pytest.approx(0.0)


@pytest.mark.anyio
async def test_save_calibration_creates_missing_shelf_and_cell_from_templates(client, tmp_path) -> None:
	grid = build_grid(tmp_path)
	worker = build_worker(grid)

	with patch('perpetual_os1_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={
				'side': 'left_module',
				'shelf_idx': 1,
				'cell_idx': 0,
				'x': 475.0,
				'y': 630.0,
				'z_offset': 0.0,
			},
		)

	assert response.status_code == 200
	assert len(grid.left_module) == 2

	new_shelf = grid.left_module[1]

	assert new_shelf.shelf_idx == 1
	assert new_shelf.shelf_height == pytest.approx(grid.left_module[0].shelf_height)
	assert new_shelf.shelf_width == pytest.approx(grid.left_module[0].shelf_width)
	assert new_shelf.divider_width == pytest.approx(grid.left_module[0].divider_width)
	assert len(new_shelf.cells) == 1
	assert new_shelf.cells[0].dimensions == grid.left_module[0].cells[0].dimensions
	assert new_shelf.cells[0].calibration_point is not None
	assert new_shelf.cells[0].calibration_point.x == pytest.approx(475.0)
	assert new_shelf.cells[0].calibration_point.y == pytest.approx(630.0)


@pytest.mark.anyio
async def test_save_calibration_rejects_negative_indices(client, tmp_path) -> None:
	grid = build_grid(tmp_path)
	worker = build_worker(grid)

	with patch('perpetual_os1_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={
				'side': 'left_module',
				'shelf_idx': -1,
				'cell_idx': 0,
				'x': 475.0,
				'y': 630.0,
				'z_offset': 0.0,
			},
		)

	assert response.status_code == 400
	assert response.json()['detail'] == 'Invalid shelf_idx: -1'


@pytest.mark.anyio
async def test_save_calibration_bootstraps_first_shelf_and_cell_on_empty_grid(client, tmp_path) -> None:
	grid = build_empty_grid(tmp_path)
	worker = build_worker(grid)

	with patch('perpetual_os1_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={
				'side': 'left_module',
				'shelf_idx': 0,
				'cell_idx': 0,
				'x': 475.0,
				'y': 630.0,
				'z_offset': 0.0,
			},
		)

	assert response.status_code == 200
	assert len(grid.left_module) == 1

	new_shelf = grid.left_module[0]

	assert new_shelf.shelf_idx == 0
	assert new_shelf.shelf_height == pytest.approx(200.0)
	assert new_shelf.shelf_width == pytest.approx(480.0)
	assert new_shelf.divider_width == pytest.approx(12.0)
	assert len(new_shelf.cells) == 1
	assert new_shelf.cells[0].dimensions == Dimensions(width=480.0, height=200.0, length=400.0)
	assert new_shelf.cells[0].calibration_point is not None
	assert new_shelf.cells[0].calibration_point.x == pytest.approx(475.0)
	assert new_shelf.cells[0].calibration_point.y == pytest.approx(630.0)


@pytest.mark.anyio
async def test_save_calibration_bootstraps_first_cell_on_empty_shelf(client, tmp_path) -> None:
	grid = build_grid_with_empty_shelf(tmp_path)
	worker = build_worker(grid)

	with patch('perpetual_os1_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={
				'side': 'left_module',
				'shelf_idx': 0,
				'cell_idx': 0,
				'x': 475.0,
				'y': 630.0,
				'z_offset': 0.0,
			},
		)

	assert response.status_code == 200
	assert len(grid.left_module[0].cells) == 1
	assert grid.left_module[0].cells[0].dimensions == Dimensions(width=480.0, height=210.0, length=400.0)
	assert grid.left_module[0].cells[0].calibration_point is not None
	assert grid.left_module[0].cells[0].calibration_point.x == pytest.approx(475.0)
	assert grid.left_module[0].cells[0].calibration_point.y == pytest.approx(630.0)
