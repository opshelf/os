import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openshelf_os.config.models import Motors


def _door_config(*, close: float = 2.0, open: float = 55.5):
	return Motors.model_validate(
		[
			{
				"motor_name": "door",
				"motor_id": 31,
				"Ip": 22,
				"rpm": 3000,
				"dr": 0,
				"mpr": 6.0,
				"accel": 1500,
				"ret_rpm": 500,
				"home_rpm": 150,
				"lim": open,
				"lower_lim": close,
			}
		]
	).by_name("door")


def _config(*, close: float = 2.0, open: float = 55.5):
	motors = Motors.model_validate([_door_config(close=close, open=open).model_dump()])
	return SimpleNamespace(robot=SimpleNamespace(io=SimpleNamespace(motors=motors)))


def _worker(motor):
	io = SimpleNamespace(
		_md=motor,
		_door_motion_lock=asyncio.Lock(),
		refresh_door_state=AsyncMock(),
	)
	robot = SimpleNamespace(_io=io, reload_parameters=MagicMock())
	return SimpleNamespace(
		_robot=robot,
		needs_home=lambda: False,
		needs_home_reason=lambda: None,
		needs_home_axes=lambda: [],
	)


@pytest.mark.anyio
async def test_get_iobox_door_position_reads_motor_limits(client) -> None:
	config = _config(close=1.5, open=58.0)

	with patch("openshelf_os_server.routers.calibration.get_root_config", return_value=config):
		response = await client.get("/api/calibration/iobox/door")

	assert response.status_code == 200
	assert response.json() == {"close": 1.5, "open": 58.0}


@pytest.mark.anyio
async def test_move_iobox_door_uses_unsaved_candidate_limits(client) -> None:
	captured_limits: list[tuple[float, float]] = []
	motor = SimpleNamespace(_params=_door_config(), move_to_position=AsyncMock())

	async def capture_move(_target: float) -> None:
		captured_limits.append((float(motor._params.lower_lim), float(motor._params.lim)))

	motor.move_to_position.side_effect = capture_move
	worker = _worker(motor)

	with patch(
		"openshelf_os_server.routers.calibration.get_robot_worker",
		AsyncMock(return_value=worker),
	):
		response = await client.post(
			"/api/calibration/iobox/door/move",
			json={"position": "close", "close": 0.0, "open": 60.0},
		)

	assert response.status_code == 200
	assert response.json() == {"success": True, "position": "close", "position_mm": 0.0}
	assert captured_limits == [(0.0, 60.0)]
	assert motor._params.lower_lim == pytest.approx(2.0)
	assert motor._params.lim == pytest.approx(55.5)
	worker._robot._io.refresh_door_state.assert_awaited_once_with(force=True)


@pytest.mark.anyio
async def test_save_iobox_door_position_persists_and_reloads_runtime(client) -> None:
	config = _config()
	worker = _worker(SimpleNamespace())

	with patch("openshelf_os_server.routers.calibration.get_root_config", return_value=config):
		with patch("openshelf_os_server.routers.calibration.save_root_config") as save_config:
			with patch(
				"openshelf_os_server.routers.calibration.get_robot_worker",
				AsyncMock(return_value=worker),
			):
				response = await client.post(
					"/api/calibration/iobox/door/save",
					json={"close": 0.5, "open": 57.0},
				)

	assert response.status_code == 200
	assert response.json() == {"success": True}
	door_config = config.robot.io.motors.by_name("door")
	assert door_config.lower_lim == pytest.approx(0.5)
	assert door_config.lim == pytest.approx(57.0)
	save_config.assert_called_once_with(config)
	worker._robot.reload_parameters.assert_called_once_with()


@pytest.mark.anyio
async def test_iobox_door_position_rejects_close_at_or_above_open(client) -> None:
	response = await client.post(
		"/api/calibration/iobox/door/save",
		json={"close": 55.0, "open": 55.0},
	)

	assert response.status_code == 400
	assert response.json()["detail"]["message"] == "IO door close position must be less than open position"
