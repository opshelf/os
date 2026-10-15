import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from fastapi import HTTPException
from hardware.servo import GripState

from openshelf_os.common import AxisCoordinate
from openshelf_os_server.routers import calibration


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
	grid = SimpleNamespace(front_module=[shelf], back_module=[])
	robot = SimpleNamespace(_grid=grid, _tool=tool)

	return SimpleNamespace(
		_robot=robot,
		needs_home=lambda: False,
		needs_home_reason=lambda: None,
		needs_home_axes=lambda: [],
	)


@pytest.mark.anyio
async def test_move_to_position_clamps_out_of_range_targets(client) -> None:
	worker = _mock_worker(stage_position=6.5)

	with patch("openshelf_os_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
		with patch("openshelf_os_server.routers.calibration._clear_motors_for_move", AsyncMock()):
			response = await client.post(
				"/api/calibration/move/position",
				json={"side": "front_module", "x": 1200.0, "y": 1600.0, "z_offset": 20.0},
			)

	assert response.status_code == 200
	assert response.json()["x"] == pytest.approx(1000.0)
	assert response.json()["y"] == pytest.approx(1500.0)
	assert response.json()["z_offset"] == pytest.approx(9.5)
	warnings = response.json()["warnings"]
	assert warnings is not None
	assert any("X axis limit reached" in w for w in warnings)
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
	worker = _mock_iobox_worker()
	calibration._iobox_current = None
	params = SimpleNamespace(
		PICKUP_POSITION_L=[100.0, 200.0],
		PICKUP_POSITION_H=[110.0, 210.0],
		Z_EXTENSION_IO=10.0,
		STAGE_EXTENSION_IO_BOX=20.0,
		PICKUP_POSITION_MULTI_ITEM=[120.0, 220.0],
		Z_EXTENSION_IO_MULTI_ITEM=30.0,
		STAGE_EXTENSION_IO_BOX_MULTI_ITEM=40.0,
	)
	config = SimpleNamespace(robot=SimpleNamespace(tool=SimpleNamespace(parameters=params)))

	try:
		with patch("openshelf_os_server.routers.calibration.get_root_config", return_value=config):
			with patch("openshelf_os_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
				with patch("openshelf_os_server.routers.calibration._clear_motors_for_move", AsyncMock()):
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
async def test_iobox_multi_item_jog_moves_only_its_own_extension(client) -> None:
	worker = _mock_iobox_worker()
	calibration._iobox_current = None
	params = SimpleNamespace(
		PICKUP_POSITION_L=[100.0, 200.0],
		PICKUP_POSITION_H=[110.0, 210.0],
		Z_EXTENSION_IO=10.0,
		STAGE_EXTENSION_IO_BOX=20.0,
		PICKUP_POSITION_MULTI_ITEM=[120.0, 220.0],
		Z_EXTENSION_IO_MULTI_ITEM=30.0,
		STAGE_EXTENSION_IO_BOX_MULTI_ITEM=40.0,
	)
	config = SimpleNamespace(robot=SimpleNamespace(tool=SimpleNamespace(parameters=params)))

	try:
		with patch("openshelf_os_server.routers.calibration.get_root_config", return_value=config):
			with patch("openshelf_os_server.routers.calibration.get_robot_worker", AsyncMock(return_value=worker)):
				with patch("openshelf_os_server.routers.calibration._clear_motors_for_move", AsyncMock()):
					response = await client.post(
						"/api/calibration/iobox/jog",
						json={"position": "multi_item", "axis": "stage", "delta": 1.5},
					)

		assert response.status_code == 200
		body = response.json()
		assert body["multi_item_stage_extension"] == pytest.approx(41.5)
		assert body["stage_extension"] == pytest.approx(20.0)
		worker._robot._tool._ms.move_to_position.assert_awaited_once_with(41.5)
	finally:
		calibration._iobox_current = None


def _mock_multi_item_worker(grip_state):
	operations = Mock()
	stage = SimpleNamespace(move_to_position=AsyncMock(), _params=SimpleNamespace(lim=170.0, lower_lim=0.0))
	z_axis = SimpleNamespace(move_to_position=AsyncMock(), _params=SimpleNamespace(lim=170.0, lower_lim=0.0))
	gripper = SimpleNamespace(
		open=AsyncMock(return_value=grip_state),
		close=AsyncMock(),
		read_state=AsyncMock(return_value=grip_state),
	)
	tool = SimpleNamespace(
		get_param=AsyncMock(side_effect=lambda name: {"Z_MOVE_RPM": 800, "Z_MOVE_ACCEL": 1200}.get(name)),
		move=AsyncMock(),
		rotate_to_position=AsyncMock(),
		retract=AsyncMock(),
		retract_empty=AsyncMock(),
		is_grip_detection_enabled=True,
		_mg=z_axis,
		_ms=stage,
		_gs=gripper,
	)
	io = SimpleNamespace(open_door=AsyncMock())
	for name, method in (
		("move", tool.move),
		("stage", stage.move_to_position),
		("z", z_axis.move_to_position),
		("grip", gripper.open),
		("release", gripper.close),
		("retract", tool.retract),
		("retract_empty", tool.retract_empty),
	):
		operations.attach_mock(method, name)
	worker = SimpleNamespace(
		_robot=SimpleNamespace(_tool=tool, _io=io),
		needs_home=lambda: False,
		needs_home_reason=lambda: None,
		needs_home_axes=lambda: [],
		set_needs_home=lambda *args, **kwargs: None,
		get_status_snapshot=AsyncMock(),
	)
	return worker, operations


def _multi_item_position():
	return calibration.IOBoxPositionResponse(
		pickup_low_x=100.0,
		pickup_low_y=200.0,
		pickup_high_x=110.0,
		pickup_high_y=210.0,
		z_extension=10.0,
		stage_extension=20.0,
		multi_item_x=120.0,
		multi_item_y=220.0,
		multi_item_z_extension=30.0,
		multi_item_stage_extension=40.0,
	)


@pytest.mark.anyio
async def test_iobox_multi_item_test_loads_the_tray_and_puts_it_back(monkeypatch) -> None:
	worker, operations = _mock_multi_item_worker(GripState(0x71, 0x9000, 0x90, 0x40))
	monkeypatch.setattr(calibration, "_iobox_current", _multi_item_position())
	monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(calibration, "get_robot_worker", AsyncMock(return_value=worker))
	monkeypatch.setattr(calibration, "_clear_motors_for_move", AsyncMock())

	assert await calibration.test_iobox_multi_item_cycle() == {"success": True}

	extend = [call.stage(40.0), call.z(30.0, rpm=800, accel=1200)]
	assert operations.mock_calls == [
		call.release(),
		call.move(AxisCoordinate(120.0, 220.0)),
		*extend,
		call.grip(),
		call.retract(),
		*extend,
		call.release(),
		call.retract_empty(),
	]


@pytest.mark.anyio
async def test_iobox_multi_item_test_stops_when_no_tray_is_gripped(monkeypatch) -> None:
	worker, operations = _mock_multi_item_worker(GripState(0xF1, 0, 0, 0))
	monkeypatch.setattr(calibration, "_iobox_current", _multi_item_position())
	monkeypatch.setattr(calibration, "ensure_laser_auto_calibration_not_running", AsyncMock())
	monkeypatch.setattr(calibration, "get_robot_worker", AsyncMock(return_value=worker))
	monkeypatch.setattr(calibration, "_clear_motors_for_move", AsyncMock())

	with pytest.raises(HTTPException) as raised:
		await calibration.test_iobox_multi_item_cycle()

	assert raised.value.status_code == 409
	assert raised.value.detail["needs_home"] is True
	worker._robot._tool.retract.assert_not_awaited()
