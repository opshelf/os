from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	Shelf,
	StorageGrid,
)
from openshelf_os_server.routers import calibration


def build_grid(*, second_cell_status: CellStatus = CellStatus.AVAILABLE) -> StorageGrid:
	def cell(cell_idx: int, status: CellStatus) -> Cell:
		return Cell(
			cell_idx=cell_idx,
			status=status,
			item=None,
			calibration_point=CalibrationCoordinate(x=120.0 * (cell_idx + 1), y=456.0, z_offset=7.5, s=0),
			dimensions=Dimensions(width=100.0, height=200.0, length=100.0),
		)

	return StorageGrid(
		front_module=[
			Shelf(
				shelf_idx=0,
				cells=[cell(0, CellStatus.AVAILABLE), cell(1, second_cell_status)],
			)
		],
		back_module=[],
		filename='/tmp/test_calibration_test_queue.json',
	)


def build_worker(grid: StorageGrid, pick: AsyncMock):
	tool = SimpleNamespace(
		pick=pick,
		pick_lift_place=AsyncMock(),
		place=AsyncMock(),
		retract=AsyncMock(),
		_ms=SimpleNamespace(get_position=AsyncMock(return_value=0.0)),
	)
	robot = SimpleNamespace(_grid=grid, _tool=tool, io_pick=AsyncMock(), io_place=AsyncMock())
	return SimpleNamespace(
		_robot=robot,
		_current_command=None,
		_demo_cycling=False,
		needs_home=MagicMock(return_value=False),
		needs_home_reason=MagicMock(return_value=None),
		needs_home_axes=MagicMock(return_value=[]),
		set_needs_home=MagicMock(),
		get_status_snapshot=AsyncMock(),
		refresh_tray_on_tool=AsyncMock(return_value=False),
		publish_event=AsyncMock(),
	)


def patch_worker(worker):
	return (
		patch('openshelf_os_server.routers.calibration.get_robot_worker', AsyncMock(return_value=worker)),
		patch('openshelf_os_server.routers.calibration._clear_motors_for_move', AsyncMock()),
	)


def published_queue_events(worker) -> list[dict]:
	events = []
	for call in worker.publish_event.await_args_list:
		payload = call.args[0]
		if payload.get('kind') == 'calibration_test_queue':
			events.append(payload['payload'])
	return events


async def await_test_queue() -> None:
	task = calibration._test_queue_task
	assert task is not None
	await task


@pytest.mark.asyncio
async def test_queue_stops_at_the_first_failing_cell(client) -> None:
	# A disabled cell must stop the walk: motion never continues past a cell the
	# interlocks reject, and the terminal failure is observable without the tab.
	grid = build_grid(second_cell_status=CellStatus.DISABLED)
	worker = build_worker(grid, AsyncMock())

	with patch_worker(worker)[0], patch_worker(worker)[1]:
		start = await client.post(
			'/api/calibration/test/queue',
			json={'side': 'front_module', 'mode': 'quick', 'cells': [{'shelf_idx': 0, 'cell_idx': 0}, {'shelf_idx': 0, 'cell_idx': 1}]},
		)
		assert start.status_code == 200
		await await_test_queue()

		assert worker._robot._tool.pick.await_count == 1

		status = await client.get('/api/calibration/test/queue')

	assert status.json()['state'] == 'failed'
	assert status.json()['failed'] == {'shelf_idx': 0, 'cell_idx': 1}
	assert 'disabled' in status.json()['error']

	events = published_queue_events(worker)
	assert events[-1]['state'] == 'failed'
	assert events[-1]['job_id'] == start.json()['job_id']


@pytest.mark.asyncio
async def test_queue_start_is_rejected_while_another_queue_runs(client) -> None:
	# Two concurrent walks would interleave motion commands; the second start must lose.
	grid = build_grid()
	gate = asyncio.Event()

	async def blocked_pick(*args, **kwargs):
		await asyncio.wait_for(gate.wait(), timeout=5)

	worker = build_worker(grid, AsyncMock(side_effect=blocked_pick))

	with patch_worker(worker)[0], patch_worker(worker)[1]:
		first = await client.post(
			'/api/calibration/test/queue',
			json={'side': 'front_module', 'mode': 'quick', 'cells': [{'shelf_idx': 0, 'cell_idx': 0}]},
		)
		assert first.status_code == 200
		assert first.json()['state'] == 'running'

		while worker._robot._tool.pick.await_count == 0:
			await asyncio.sleep(0.01)

		second = await client.post(
			'/api/calibration/test/queue',
			json={'side': 'front_module', 'mode': 'quick', 'cells': [{'shelf_idx': 0, 'cell_idx': 1}]},
		)
		assert second.status_code == 409

		gate.set()
		await await_test_queue()

	assert worker._robot._tool.pick.await_count == 1


@pytest.mark.asyncio
async def test_abort_stops_the_queue_before_the_next_cell(client) -> None:
	# Abort takes effect between cells: the in-flight cell finishes its pick/place and
	# no later cell starts.
	grid = build_grid()
	gate = asyncio.Event()

	async def gated_pick(*args, **kwargs):
		# Hold the first cell in-flight so the abort lands mid-walk.
		await asyncio.wait_for(gate.wait(), timeout=5)

	worker = build_worker(grid, AsyncMock(side_effect=gated_pick))

	with patch_worker(worker)[0], patch_worker(worker)[1]:
		start = await client.post(
			'/api/calibration/test/queue',
			json={'side': 'front_module', 'mode': 'quick', 'cells': [{'shelf_idx': 0, 'cell_idx': 0}, {'shelf_idx': 0, 'cell_idx': 1}]},
		)
		assert start.status_code == 200

		while worker._robot._tool.pick.call_count == 0:
			await asyncio.sleep(0.01)

		abort = await client.post('/api/calibration/test/queue/abort')
		assert abort.status_code == 200

		gate.set()
		await await_test_queue()

	assert worker._robot._tool.pick.await_count == 1

	events = published_queue_events(worker)
	assert events[-1]['state'] == 'cancelled'
