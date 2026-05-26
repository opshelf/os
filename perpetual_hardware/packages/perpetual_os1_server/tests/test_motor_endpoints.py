import asyncio
import pytest
from httpx import AsyncClient
from unittest.mock import patch, MagicMock, AsyncMock


def _mock_settings():
	mock = MagicMock()
	mock.robot.enabled = True
	return mock


def _mock_motor(position: float = 100.0):
	motor = AsyncMock()
	motor.get_position = AsyncMock(return_value=position)
	motor.move_to_position = AsyncMock()
	motor.clear_alarm = AsyncMock()
	motor.home = AsyncMock()
	return motor


def _mock_worker(motors: dict[str, AsyncMock] | None = None):
	worker = AsyncMock()
	worker.get_motor_names = MagicMock(return_value={
		"io": ["door"],
		"tool": ["x_axis", "y_axis", "z_axis", "stage", "rotate"],
	})
	worker.clear_needs_home_axes = MagicMock()
	worker.needs_home = MagicMock(return_value=False)
	worker.needs_home_axes = MagicMock(return_value=[])
	worker.needs_home_reason = MagicMock(return_value=None)
	worker.clear_needs_home = MagicMock()
	worker.get_status_snapshot = AsyncMock(return_value={})
	worker._current_command = None
	worker._demo_cycling = False

	if motors is None:
		motors = {
			"door": _mock_motor(50.0),
			"x_axis": _mock_motor(100.0),
			"y_axis": _mock_motor(200.0),
			"z_axis": _mock_motor(150.0),
			"stage": _mock_motor(75.0),
			"rotate": _mock_motor(0.0),
		}

	robot = MagicMock()
	robot._get_motor_by_name = MagicMock(side_effect=lambda name: motors.get(name))
	robot._tool = MagicMock()
	robot._tool.ensure_safe_rotation_stage_extension = AsyncMock()
	robot._tool.ensure_safe_rotation_x_position = AsyncMock(return_value=None)
	robot._tool.restore_safe_rotation_x_position = AsyncMock()
	robot._tool.is_rotation_at_position = AsyncMock(return_value=False)
	robot._tool.rotate_to_home_orientation = AsyncMock()
	worker._robot = robot

	return worker


@pytest.mark.anyio
async def test_probe_motors_all_responsive(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/probe")

	assert response.status_code == 200
	data = response.json()
	assert "results" in data
	results = {r["motor_name"]: r for r in data["results"]}

	assert results["door"]["responsive"] is True
	assert results["door"]["position_mm"] == pytest.approx(50.0)
	assert results["x_axis"]["responsive"] is True
	assert results["x_axis"]["position_mm"] == pytest.approx(100.0)
	assert results["y_axis"]["responsive"] is True
	assert results["y_axis"]["position_mm"] == pytest.approx(200.0)


@pytest.mark.anyio
async def test_probe_motors_with_failure(client: AsyncClient) -> None:
	failing_motor = AsyncMock()
	failing_motor.get_position = AsyncMock(side_effect=Exception("communication error"))

	motors = {
		"door": failing_motor,
		"x_axis": _mock_motor(100.0),
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": _mock_motor(75.0),
		"rotate": _mock_motor(0.0),
	}
	mock_worker = _mock_worker(motors)

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/probe")

	assert response.status_code == 200
	data = response.json()
	results = {r["motor_name"]: r for r in data["results"]}

	assert results["door"]["responsive"] is False
	assert results["door"]["error"] == "communication error"
	assert results["x_axis"]["responsive"] is True


@pytest.mark.anyio
async def test_probe_motors_robot_disabled(client: AsyncClient) -> None:
	mock_settings = MagicMock()
	mock_settings.robot.enabled = False

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=mock_settings):
		response = await client.post("/api/robot/debug/motors/probe")

	assert response.status_code == 503


@pytest.mark.anyio
async def test_move_motor_success(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/x_axis/move",
				json={"position_mm": 250.5},
			)

	assert response.status_code == 200
	data = response.json()
	assert data["success"] is True
	assert data["motor"] == "x_axis"
	assert data["position_mm"] == pytest.approx(250.5)


@pytest.mark.anyio
async def test_move_motor_conflict_while_move_running(client: AsyncClient) -> None:
	started = asyncio.Event()
	finish = asyncio.Event()

	async def did_move(_position: float) -> None:
		started.set()
		await finish.wait()

	motor = _mock_motor(100.0)
	motor.move_to_position = AsyncMock(side_effect=did_move)
	motors = {
		"door": _mock_motor(50.0),
		"x_axis": motor,
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": _mock_motor(75.0),
		"rotate": _mock_motor(0.0),
	}
	mock_worker = _mock_worker(motors)

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			first = asyncio.create_task(client.post(
				"/api/robot/debug/motors/x_axis/move",
				json={"position_mm": 250.5},
			))
			await started.wait()
			second = await client.post(
				"/api/robot/debug/motors/y_axis/move",
				json={"position_mm": 125.0},
			)
			finish.set()
			first_response = await first

	assert first_response.status_code == 200
	assert second.status_code == 409
	assert second.json()["detail"]["code"] == "movement_in_progress"


@pytest.mark.anyio
async def test_robot_command_conflicts_while_manual_move_running(client: AsyncClient) -> None:
	started = asyncio.Event()
	finish = asyncio.Event()

	async def did_move(_position: float) -> None:
		started.set()
		await finish.wait()

	motor = _mock_motor(100.0)
	motor.move_to_position = AsyncMock(side_effect=did_move)
	motors = {
		"door": _mock_motor(50.0),
		"x_axis": motor,
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": _mock_motor(75.0),
		"rotate": _mock_motor(0.0),
	}
	mock_worker = _mock_worker(motors)
	mock_worker.enqueue_command = AsyncMock()

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			move_task = asyncio.create_task(client.post(
				"/api/robot/debug/motors/x_axis/move",
				json={"position_mm": 250.5},
			))
			await started.wait()
			command_response = await client.post(
				"/api/robot/commands",
				json={"command": "home"},
			)
			finish.set()
			move_response = await move_task

	assert move_response.status_code == 200
	assert command_response.status_code == 409
	assert command_response.json()["detail"]["code"] == "movement_in_progress"
	mock_worker.enqueue_command.assert_not_awaited()


@pytest.mark.anyio
async def test_move_motor_not_found(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/nonexistent/move",
				json={"position_mm": 100.0},
			)

	assert response.status_code == 404
	assert "not found" in response.json()["detail"].lower()


@pytest.mark.anyio
async def test_move_motor_failure(client: AsyncClient) -> None:
	failing_motor = AsyncMock()
	failing_motor.move_to_position = AsyncMock(side_effect=Exception("motor timeout"))

	motors = {
		"door": _mock_motor(50.0),
		"x_axis": failing_motor,
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": _mock_motor(75.0),
		"rotate": _mock_motor(0.0),
	}
	mock_worker = _mock_worker(motors)

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/x_axis/move",
				json={"position_mm": 100.0},
			)

	assert response.status_code == 503
	detail = response.json()["detail"]
	assert detail["code"] == "motor_move_failed"
	assert "motor timeout" in detail["message"].lower()


@pytest.mark.anyio
async def test_move_motor_robot_disabled(client: AsyncClient) -> None:
	mock_settings = MagicMock()
	mock_settings.robot.enabled = False

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=mock_settings):
		response = await client.post(
			"/api/robot/debug/motors/x_axis/move",
			json={"position_mm": 100.0},
		)

	assert response.status_code == 503


@pytest.mark.anyio
async def test_move_rotate_motor_extends_stage_before_rotation(client: AsyncClient) -> None:
	rotate_motor = _mock_motor(0.0)
	motors = {
		"door": _mock_motor(50.0),
		"x_axis": _mock_motor(100.0),
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": _mock_motor(75.0),
		"rotate": rotate_motor,
	}
	mock_worker = _mock_worker(motors)

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/rotate/move",
				json={"position_mm": 42.0},
			)

	assert response.status_code == 200
	mock_worker._robot._tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	rotate_motor.move_to_position.assert_awaited_once_with(42.0)


@pytest.mark.anyio
async def test_home_rotate_motor_extends_stage_before_rotation(client: AsyncClient) -> None:
	rotate_motor = _mock_motor(0.0)
	motors = {
		"door": _mock_motor(50.0),
		"x_axis": _mock_motor(100.0),
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": _mock_motor(75.0),
		"rotate": rotate_motor,
	}
	mock_worker = _mock_worker(motors)

	with patch("perpetual_os1_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("perpetual_os1_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/rotate/home")

	assert response.status_code == 200
	mock_worker._robot._tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	rotate_motor.clear_alarm.assert_awaited_once()
	rotate_motor.home.assert_awaited_once()
	mock_worker._robot._tool.rotate_to_home_orientation.assert_awaited_once_with(
		should_ensure_safe_stage_extension=False,
	)
