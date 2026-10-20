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


@pytest.mark.asyncio
async def test_tray_home_in_maintenance_stays_testable(client) -> None:
	"""Startup marks tray homes maintenance; their tray must still be testable for grip tuning."""
	grid = build_grid(is_occupied=False, cell_status=CellStatus.MAINTENANCE)
	worker = build_worker(grid)
	tray = SimpleNamespace(
		barcode='0000000003',
		home_location=SimpleNamespace(side='front_module', shelf_idx=0, cell_idx=0),
	)
	config = SimpleNamespace(storage=SimpleNamespace(bins=[tray]))

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		with patch('openshelf_os_server.routers.calibration._clear_motors_for_move', AsyncMock()):
			with patch('openshelf_os_server.core.bin_storage.get_root_config', return_value=config):
				response = await client.post(
					'/api/calibration/test',
					json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0},
				)

	assert response.status_code == 200
	assert worker._robot._tool.pick.await_count == 1
	assert worker._robot._tool.place.await_count == 1


@pytest.mark.asyncio
async def test_cycle_test_rejects_tray_home_in_maintenance(client) -> None:
	"""The IO cycle is a single-item transfer and would write a fake item into the tray home."""
	grid = build_grid(is_occupied=False, cell_status=CellStatus.MAINTENANCE)
	worker = build_worker(grid)
	tray = SimpleNamespace(
		barcode='0000000003',
		home_location=SimpleNamespace(side='front_module', shelf_idx=0, cell_idx=0),
	)
	config = SimpleNamespace(storage=SimpleNamespace(bins=[tray]))

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		with patch('openshelf_os_server.routers.calibration._clear_motors_for_move', AsyncMock()):
			with patch('openshelf_os_server.core.bin_storage.get_root_config', return_value=config):
				response = await client.post(
					'/api/calibration/test/cycle',
					json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0},
				)

	assert response.status_code == 400
	assert worker._robot.io_place.await_count == 0
	assert worker._robot.io_pick.await_count == 0


@pytest.mark.asyncio
async def test_cycle_test_retracts_before_the_io_cycle_rotates(client) -> None:
	"""io_place/io_pick rotate before they retract, so the cycle has to retract up front."""
	grid = build_grid(is_occupied=True)
	worker = build_worker(grid)
	worker._robot._io = SimpleNamespace(
		ensure_door_homed=AsyncMock(),
		open_door=AsyncMock(),
		close_door=AsyncMock(),
		home_door_after_cycle=AsyncMock(),
	)
	worker._robot.io_place.side_effect = None
	worker._robot.io_pick.side_effect = None

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		with patch('openshelf_os_server.routers.calibration._clear_motors_for_move', AsyncMock()):
			with patch('openshelf_os_server.routers.calibration._retract_tool_for_move', AsyncMock()) as retract:
				response = await client.post(
					'/api/calibration/test/cycle',
					json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0},
				)

	assert response.status_code == 200
	retract.assert_awaited_once()
	assert worker._robot.io_place.await_count == 1
	assert worker._robot.io_pick.await_count == 1


@pytest.mark.asyncio
async def test_saving_a_typed_z_stamps_the_shelf_stage_extension(client) -> None:
	"""A z typed on the cell page is saved with the stage retracted; stamping that live position
	put the datum 43mm from the shelf extension and made auto calibration refuse it (lb4a-3)."""
	grid = build_grid(is_occupied=False)
	worker = build_worker(grid)
	worker._robot._tool._ms = SimpleNamespace(get_position=AsyncMock(return_value=3.06))
	worker._robot._tool.get_param = AsyncMock(side_effect=lambda name: {'STAGE_EXTENSION_SHELF_FRONT': 46.0}[name])

	with patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)):
		response = await client.post(
			'/api/calibration/save',
			json={'side': 'front_module', 'shelf_idx': 0, 'cell_idx': 0, 'x': 123.0, 'y': 456.0, 'z_offset': 8.5},
		)

	assert response.status_code == 200
	point = grid.front_module[0].cells[0].calibration_point
	assert point.z_offset == 8.5
	assert point.z_captured_at_stage_mm == 46.0
