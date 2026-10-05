from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os.storage import CalibrationCoordinate, Cell, CellItem, CellStatus, Dimensions, Shelf, StorageGrid


def build_grid(*, is_occupied: bool, cell_status: CellStatus | None = None) -> StorageGrid:
	status = CellStatus.OCCUPIED if is_occupied is True else CellStatus.AVAILABLE

	if cell_status is not None:
		status = cell_status

	item = None

	if is_occupied is True:
		item = CellItem(
			dimensions=Dimensions(width=80.0, height=120.0, length=80.0),
			data={'name': 'existing item'},
			barcode='123456',
		)

	return StorageGrid(
		front_module=[
			Shelf(
				shelf_idx=0,
				cells=[
					Cell(
						cell_idx=0,
						status=status,
						item=item,
						calibration_point=CalibrationCoordinate(x=123.0, y=456.0, z_offset=7.5, s=0),
						dimensions=Dimensions(width=100.0, height=200.0, length=100.0),
					)
				],
			)
		],
		back_module=[],
		filename='/tmp/test_calibration_storage.json',
	)


def build_worker(grid: StorageGrid):
	tool = SimpleNamespace(
		pick=AsyncMock(),
		pick_lift_place=AsyncMock(),
		place=AsyncMock(),
		retract=AsyncMock(),
		_ms=SimpleNamespace(get_position=AsyncMock(return_value=0.0)),
	)

	call_order: list[str] = []

	async def did_io_pick(*args, **kwargs):
		call_order.append('store')

	async def did_io_place(*args, **kwargs):
		call_order.append('retrieve')

	robot = SimpleNamespace(
		_grid=grid,
		_tool=tool,
		io_pick=AsyncMock(side_effect=did_io_pick),
		io_place=AsyncMock(side_effect=did_io_place),
		call_order=call_order,
	)

	return SimpleNamespace(
		_robot=robot,
		_current_command=None,
		_demo_cycling=False,
		needs_home=MagicMock(return_value=False),
		needs_home_reason=MagicMock(return_value=None),
		needs_home_axes=MagicMock(return_value=[]),
		set_needs_home=MagicMock(),
		get_status_snapshot=AsyncMock(),
	)


@pytest.mark.asyncio
async def test_cycle_test_runs_retrieve_then_store_for_occupied_cell(client) -> None:
	grid = build_grid(is_occupied=True)
	worker = build_worker(grid)
	original_item = grid.front_module[0].cells[0].item.model_copy(deep=True)

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		with patch('openshelf_os_server.routers.calibration._clear_motors_for_move', AsyncMock()):
			response = await client.post(
				'/api/calibration/test/cycle',
				json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0},
			)

	assert response.status_code == 200
	assert worker._robot.io_place.await_count == 1
	assert worker._robot.io_pick.await_count == 1
	assert worker._robot.call_order == ['retrieve', 'store']

	cell = grid.front_module[0].cells[0]

	assert cell.status == CellStatus.OCCUPIED
	assert cell.item == original_item


@pytest.mark.asyncio
async def test_cycle_test_rejects_when_home_is_required(client) -> None:
	grid = build_grid(is_occupied=False)
	worker = build_worker(grid)
	worker.needs_home = MagicMock(return_value=True)
	worker.needs_home_reason = MagicMock(return_value='test home required')
	worker.needs_home_axes = MagicMock(return_value=['stage'])

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/test/cycle',
			json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0},
		)

	assert response.status_code == 409
	assert response.json()['detail']['needs_home'] is True
	assert response.json()['detail']['reason'] == 'test home required'


@pytest.mark.parametrize('cell_status', [CellStatus.DISABLED, CellStatus.MAINTENANCE])
@pytest.mark.parametrize('endpoint', ['/api/calibration/test', '/api/calibration/test/cycle'])
@pytest.mark.asyncio
async def test_tests_reject_cells_that_are_not_testable(client, endpoint: str, cell_status: CellStatus) -> None:
	grid = build_grid(is_occupied=False, cell_status=cell_status)
	worker = build_worker(grid)

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		with patch('openshelf_os_server.routers.calibration._clear_motors_for_move', AsyncMock()):
			response = await client.post(
				endpoint,
				json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0},
			)

	assert response.status_code == 400
	assert cell_status.value in response.json()['detail']['message']

	assert worker._robot._tool.pick.await_count == 0
	assert worker._robot._tool.place.await_count == 0
	assert worker._robot.io_pick.await_count == 0
	assert worker._robot.io_place.await_count == 0

	assert grid.front_module[0].cells[0].status == cell_status


