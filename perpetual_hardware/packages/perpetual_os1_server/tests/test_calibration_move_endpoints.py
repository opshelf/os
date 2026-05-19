import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _mock_worker(stage_position: float):
	tool = SimpleNamespace(
		move=AsyncMock(),
		rotate_to_position=AsyncMock(),
		get_param=AsyncMock(
			side_effect=lambda name: {
				"Z_EXTENSION_PLACE": 160.5,
				"Z_MOVE_RPM": 800,
				"Z_MOVE_ACCEL": 1200,
				"STAGE_EXTENSION_SHELF_FRONT": 45.0,
				"STAGE_EXTENSION_SHELF_BACK": 40.0,
			}.get(name)
		),
		_mx=SimpleNamespace(_params=SimpleNamespace(lim=1000.0, lower_lim=0.0)),
		_my=SimpleNamespace(_params=SimpleNamespace(lim=1500.0, lower_lim=0.0)),
		_mg=SimpleNamespace(
			move_to_position=AsyncMock(),
			_params=SimpleNamespace(lim=170.0, lower_lim=0.0),
		),
		_ms=SimpleNamespace(get_position=AsyncMock(return_value=stage_position)),
	)

	cell = SimpleNamespace(
		calibration_point=SimpleNamespace(x=123.0, y=456.0, z_offset=7.5),
	)
	shelf = SimpleNamespace(cells=[cell])
	grid = SimpleNamespace(left_module=[shelf], right_module=[])
	robot = SimpleNamespace(_grid=grid, _tool=tool)

	return SimpleNamespace(
		_robot=robot,
		needs_home=lambda: False,
		needs_home_reason=lambda: None,
		needs_home_axes=lambda: [],
	)


@pytest.mark.anyio
async def test_move_to_cell_reports_actual_stage_position(client) -> None:
	worker = _mock_worker(stage_position=3.0)

	with patch("perpetual_os1_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		with patch("perpetual_os1_server.routers.calibration._clear_motors_for_move", AsyncMock()):
			with patch("perpetual_os1_server.routers.calibration._get_shelf", return_value=worker._robot._grid.left_module[0]):
				with patch("perpetual_os1_server.routers.calibration._get_cell", return_value=worker._robot._grid.left_module[0].cells[0]):
					response = await client.post(
						"/api/calibration/move",
						json={"side": "left_module", "shelf_idx": 12, "cell_idx": 0},
					)

	assert response.status_code == 200
	assert response.json()["slide_extension"] == pytest.approx(3.0)


@pytest.mark.anyio
async def test_move_to_position_starts_manual_calibration_session(client) -> None:
	worker = _mock_worker(stage_position=6.5)

	with patch("perpetual_os1_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		with patch("perpetual_os1_server.routers.calibration._clear_motors_for_move", AsyncMock()):
			response = await client.post(
				"/api/calibration/move/position",
				json={"side": "right_module", "x": 475.0, "y": 630.0, "z_offset": 0.0},
			)

	assert response.status_code == 200
	assert response.json()["x"] == pytest.approx(475.0)
	assert response.json()["y"] == pytest.approx(630.0)
	assert response.json()["slide_extension"] == pytest.approx(6.5)

	move_coordinate = worker._robot._tool.move.await_args.args[0]

	assert move_coordinate.x == pytest.approx(475.0)
	assert move_coordinate.y == pytest.approx(630.0)
	assert move_coordinate.s == 1
	worker._robot._tool.rotate_to_position.assert_awaited_once_with("back")


@pytest.mark.anyio
async def test_move_to_position_clamps_out_of_range_targets(client) -> None:
	worker = _mock_worker(stage_position=6.5)

	with patch("perpetual_os1_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		with patch("perpetual_os1_server.routers.calibration._clear_motors_for_move", AsyncMock()):
			response = await client.post(
				"/api/calibration/move/position",
				json={"side": "left_module", "x": 1200.0, "y": 1600.0, "z_offset": 20.0},
			)

	assert response.status_code == 200
	assert response.json()["x"] == pytest.approx(1000.0)
	assert response.json()["y"] == pytest.approx(1500.0)
	assert response.json()["z_offset"] == pytest.approx(9.5)
	assert len(response.json()["warnings"]) == 3

	move_coordinate = worker._robot._tool.move.await_args.args[0]

	assert move_coordinate.x == pytest.approx(1000.0)
	assert move_coordinate.y == pytest.approx(1500.0)
	assert move_coordinate.z_offset == pytest.approx(9.5)


def _mock_iobox_worker():
	z_motor = SimpleNamespace(
		get_position=AsyncMock(return_value=10.123456),
		move_to_position=AsyncMock(),
		_params=SimpleNamespace(lim=170.0, lower_lim=0.0),
	)
	tool = SimpleNamespace(
		get_param=AsyncMock(side_effect=lambda name: {"Z_MOVE_RPM": 800, "Z_MOVE_ACCEL": 1200}.get(name)),
		_mx=SimpleNamespace(_params=SimpleNamespace(lim=1000.0, lower_lim=0.0)),
		_my=SimpleNamespace(_params=SimpleNamespace(lim=1500.0, lower_lim=0.0)),
		_mg=z_motor,
		_ms=SimpleNamespace(move_to_position=AsyncMock(), _params=SimpleNamespace(lim=170.0, lower_lim=0.0)),
	)
	robot = SimpleNamespace(_tool=tool)

	return SimpleNamespace(
		_robot=robot,
		needs_home=lambda: False,
		needs_home_reason=lambda: None,
		needs_home_axes=lambda: [],
	)


@pytest.mark.anyio
async def test_iobox_jog_initializes_cached_calibration_without_deadlock(client) -> None:
	from perpetual_os1_server.routers import calibration

	worker = _mock_iobox_worker()
	calibration._iobox_current = None
	params = SimpleNamespace(
		PICKUP_POSITION_L=[100.0, 200.0],
		PICKUP_POSITION_H=[110.0, 210.0],
		Z_EXTENSION_IO=10.0,
		STAGE_EXTENSION_IO_BOX=20.0,
	)
	config = SimpleNamespace(robot=SimpleNamespace(tool=SimpleNamespace(parameters=params)))

	try:
		with patch("perpetual_os1_server.routers.calibration.get_root_config", return_value=config):
			with patch("perpetual_os1_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
				with patch("perpetual_os1_server.routers.calibration._clear_motors_for_move", AsyncMock()):
					response = await asyncio.wait_for(
						client.post(
							"/api/calibration/iobox/jog",
							json={"position": "low", "axis": "z", "delta": 0.5},
						),
						timeout=1.0,
					)

		assert response.status_code == 200
		assert response.json()["z_extension"] == pytest.approx(10.5)
	finally:
		calibration._iobox_current = None


@pytest.mark.anyio
async def test_iobox_z_jog_uses_calibration_value_and_rounds_response(client) -> None:
	from perpetual_os1_server.routers import calibration

	worker = _mock_iobox_worker()
	calibration._iobox_current = calibration.IOBoxPositionResponse(
		pickup_low_x=100.0,
		pickup_low_y=200.0,
		pickup_high_x=110.0,
		pickup_high_y=210.0,
		z_extension=10.123456,
		stage_extension=20.0,
	)

	with patch("perpetual_os1_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		with patch("perpetual_os1_server.routers.calibration._clear_motors_for_move", AsyncMock()):
			response = await client.post(
				"/api/calibration/iobox/jog",
				json={"position": "low", "axis": "z", "delta": 0.5},
			)

	assert response.status_code == 200
	assert response.json()["z_extension"] == pytest.approx(10.62)
	worker._robot._tool._mg.get_position.assert_not_awaited()
	worker._robot._tool._mg.move_to_position.assert_awaited_once_with(10.62, rpm=800, accel=1200)

	calibration._iobox_current = None
