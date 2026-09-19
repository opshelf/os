from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from openshelf_os.storage import CalibrationCoordinate, Cell, CellStatus, Dimensions, Shelf, StorageGrid
from openshelf_os_server.core import bin_storage
from openshelf_os_server.routers import calibration


def build_tool_parameters() -> calibration.Parameters:
	return calibration.Parameters.model_validate(
		{
			"IDLE_POSITION": [0, 0],
			"PICKUP_POSITION_L": [100, 200],
			"PICKUP_POSITION_H": [110, 210],
			"SCAN_POSITION": [0, 0],
			"STAGE_EXTENSION_IO_BOX": 20.0,
			"STAGE_NUDGE": 1.0,
			"STAGE_EXTENSION_SHELF_FRONT": 30.0,
			"STAGE_EXTENSION_SHELF_BACK": 31.0,
			"Z_EXTENSION_PICK": 1.0,
			"Z_EXTENSION_PLACE": 2.0,
			"Z_EXTENSION_IO": 3.0,
			"Z_MOVE_RPM": 800,
			"Z_MOVE_ACCEL": 1200,
			"ROTATION_FRONT": 45.0,
			"ROTATION_BACK": 135.0,
		}
	)


def build_parameter_config():
	return SimpleNamespace(robot=SimpleNamespace(tool=SimpleNamespace(parameters=build_tool_parameters())))


def build_parameter_worker(stage_limit: float = 170.0):
	tool = SimpleNamespace(
		_ms=SimpleNamespace(_params=SimpleNamespace(lim=stage_limit, lower_lim=0.0)),
		_gs=SimpleNamespace(close=AsyncMock()),
		home=AsyncMock(),
	)
	robot = SimpleNamespace(
		_tool=tool,
		_io=SimpleNamespace(home_door=AsyncMock()),
		reload_parameters=Mock(),
	)
	return SimpleNamespace(
		_robot=robot,
		clear_needs_home=Mock(),
		get_status_snapshot=AsyncMock(),
	)




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

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
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
@pytest.mark.parametrize("cell_status", [CellStatus.OCCUPIED, CellStatus.MAINTENANCE])
async def test_save_calibration_updates_tray_home_cell(client, tmp_path, monkeypatch, cell_status) -> None:
	grid = build_grid(tmp_path)
	cell = grid.left_module[0].cells[0]
	cell.status = cell_status
	worker = build_worker(grid)
	configured_bin = SimpleNamespace(
		home_location=SimpleNamespace(side="left_module", shelf_idx=0, cell_idx=0)
	)
	monkeypatch.setattr(
		bin_storage,
		"get_root_config",
		lambda: SimpleNamespace(storage=SimpleNamespace(bins=[configured_bin])),
	)

	with patch("openshelf_os_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		response = await client.post(
			"/api/calibration/save",
			json={
				"side": "left_module",
				"shelf_idx": 0,
				"cell_idx": 0,
				"x": 475.0,
				"y": 630.0,
				"z_offset": 0.0,
				"status": "available",
				"is_manual": True,
			},
		)

	assert response.status_code == 200
	assert cell.status == CellStatus.AVAILABLE
	assert cell.is_manual is True
	assert cell.calibration_point.x == 475.0
	assert cell.calibration_point.y == 630.0
	assert cell.calibration_point.z_offset == 0.0
	assert (tmp_path / "storage-grid.json").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("cell_status", [CellStatus.AVAILABLE, CellStatus.OCCUPIED])
async def test_calibration_test_accepts_tray_homes(tmp_path, cell_status) -> None:
	grid = build_grid(tmp_path)
	cell = grid.left_module[0].cells[0]
	cell.status = cell_status
	location = calibration.StorageModuleCellLocation(
		side=calibration.StorageModuleSide.LEFT,
		shelf_idx=0,
		cell_idx=0,
	)

	assert await calibration._ensure_cell_is_testable(grid, location) is cell


@pytest.mark.anyio
@pytest.mark.parametrize("cell_status", [CellStatus.DISABLED, CellStatus.MAINTENANCE])
async def test_calibration_test_rejects_untestable_cell_status(tmp_path, cell_status) -> None:
	grid = build_grid(tmp_path)
	grid.left_module[0].cells[0].status = cell_status
	location = calibration.StorageModuleCellLocation(
		side=calibration.StorageModuleSide.LEFT,
		shelf_idx=0,
		cell_idx=0,
	)

	with pytest.raises(HTTPException) as error:
		await calibration._ensure_cell_is_testable(grid, location)

	assert error.value.status_code == 400


@pytest.mark.anyio
async def test_save_calibration_creates_missing_shelf_and_cell_from_templates(client, tmp_path) -> None:
	grid = build_grid(tmp_path)
	worker = build_worker(grid)

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
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

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
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
async def test_iobox_save_persists_reloads_and_updates_cache_after_admission(monkeypatch) -> None:
	config = build_parameter_config()
	worker = build_parameter_worker()
	save_config = Mock()
	monkeypatch.setattr(calibration, "get_root_config", lambda: config)
	monkeypatch.setattr(calibration, "save_root_config", save_config)
	monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(calibration, "get_robot_worker", AsyncMock(return_value=worker))
	calibration._iobox_current = None

	try:
		assert await calibration.save_iobox_calibration(
			calibration.IOBoxSaveRequest(
				pickup_low_x=120.0,
				pickup_low_y=220.0,
				pickup_high_x=130.0,
				pickup_high_y=230.0,
				z_extension=10.0,
				stage_extension=25.0,
			)
		) == {"success": True}

		params = config.robot.tool.parameters
		assert params.PICKUP_POSITION_L == (120, 220)
		assert params.PICKUP_POSITION_H == (130, 230)
		assert params.Z_EXTENSION_IO == 10.0
		assert params.STAGE_EXTENSION_IO_BOX == 25.0
		assert save_config.call_args.args == (config,)
		assert worker._robot.reload_parameters.call_count == 1
		assert calibration._iobox_current is not None
		assert calibration._iobox_current.pickup_low_x == 120.0
		assert calibration._iobox_current.stage_extension == 25.0
	finally:
		calibration._iobox_current = None


@pytest.mark.anyio
@pytest.mark.parametrize(
	("save_function", "save_request", "expected_parameters"),
	[
		(
			calibration.save_rotation_angles,
			calibration.RotateSaveRequest(front=50.0, back=130.0),
			{"ROTATION_FRONT": 50.0, "ROTATION_BACK": 130.0},
		),
		(
			calibration.save_shelf_stage_extension,
			calibration.ShelfStageExtensionSaveRequest(front=40.0, back=41.0),
			{"STAGE_EXTENSION_SHELF_FRONT": 40.0, "STAGE_EXTENSION_SHELF_BACK": 41.0},
		),
	],
)
async def test_parameter_saves_persist_and_reload_after_admission(
	monkeypatch, save_function, save_request, expected_parameters
) -> None:
	config = build_parameter_config()
	worker = build_parameter_worker()
	save_config = Mock()
	monkeypatch.setattr(calibration, "get_root_config", lambda: config)
	monkeypatch.setattr(calibration, "save_root_config", save_config)
	monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(calibration, "get_robot_worker", AsyncMock(return_value=worker))

	assert await save_function(save_request) == {"success": True}
	for name, value in expected_parameters.items():
		assert getattr(config.robot.tool.parameters, name) == value
	assert save_config.call_args.args == (config,)
	assert worker._robot.reload_parameters.call_count == 1


@pytest.mark.anyio
async def test_save_calibration_bootstraps_first_shelf_and_cell_on_empty_grid(client, tmp_path) -> None:
	grid = build_empty_grid(tmp_path)
	worker = build_worker(grid)

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
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

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
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


@pytest.mark.anyio
async def test_save_calibration_updates_dimensions(client, tmp_path) -> None:
	grid = build_grid(tmp_path)
	worker = build_worker(grid)

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={
				'side': 'left_module',
				'shelf_idx': 0,
				'cell_idx': 0,
				'x': 475.0,
				'y': 630.0,
				'z_offset': 0.0,
				'dimensions': {'width': 30.0, 'height': 25.0, 'length': 350.0},
			},
		)

	assert response.status_code == 200

	cell = grid.left_module[0].cells[0]
	assert cell.dimensions == Dimensions(width=30.0, height=25.0, length=350.0)
	assert cell.calibration_point is not None
	assert cell.calibration_point.x == pytest.approx(475.0)


@pytest.mark.anyio
async def test_save_calibration_keeps_dimensions_when_omitted(client, tmp_path) -> None:
	grid = build_grid(tmp_path)
	original = grid.left_module[0].cells[0].dimensions
	worker = build_worker(grid)

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
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
	assert grid.left_module[0].cells[0].dimensions == original
