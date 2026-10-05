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
	worker.clear_crash_state = MagicMock()
	worker.get_status_snapshot = AsyncMock(return_value={})
	worker._current_command = None
	worker._demo_cycling = False
	worker._is_crashed = False
	worker._crash_reason = None

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

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
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

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
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

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
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

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/rotate/home")

	assert response.status_code == 200
	mock_worker._robot._tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	rotate_motor.clear_alarm.assert_awaited_once()
	rotate_motor.home.assert_awaited_once()
	mock_worker._robot._tool.rotate_to_home_orientation.assert_awaited_once_with(
		should_ensure_safe_stage_extension=False,
	)


@pytest.mark.anyio
async def test_home_motor_clears_matching_crash_state(client: AsyncClient) -> None:
	stage_motor = _mock_motor(75.0)
	stage_motor._motor_id = 30
	motors = {
		"door": _mock_motor(50.0),
		"x_axis": _mock_motor(100.0),
		"y_axis": _mock_motor(200.0),
		"z_axis": _mock_motor(150.0),
		"stage": stage_motor,
		"rotate": _mock_motor(0.0),
	}
	mock_worker = _mock_worker(motors)
	mock_worker._is_crashed = True
	mock_worker._crash_reason = "Tool retraction failed: Movement failed for motor 30 (status=FAULT)"

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/stage/home")

	assert response.status_code == 200
	stage_motor.clear_alarm.assert_awaited_once()
	stage_motor.home.assert_awaited_once()
	mock_worker.clear_crash_state.assert_called_once_with()
