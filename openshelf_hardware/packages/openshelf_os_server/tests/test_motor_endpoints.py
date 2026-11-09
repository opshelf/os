import asyncio
import pytest
from httpx import AsyncClient
from unittest.mock import patch, MagicMock, AsyncMock

from openshelf_os.exceptions import MotorError
from openshelf_os.fridge import FridgeSafetyError


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
	worker.needs_reset = MagicMock(return_value=False)
	worker.needs_home_axes = MagicMock(return_value=[])
	worker.needs_home_reason = MagicMock(return_value=None)
	worker.clear_needs_home = MagicMock()
	worker.clear_crash_state = MagicMock()
	worker.get_status_snapshot = AsyncMock(return_value={})
	worker.refresh_tray_on_tool = AsyncMock(return_value=False)
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
	robot._tool.is_fully_retracted = AsyncMock(return_value=True)
	robot._fridge = None
	worker._robot = robot

	return worker


@pytest.mark.anyio
async def test_probe_motors_all_responsive(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
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

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
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

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=mock_settings):
		response = await client.post("/api/robot/debug/motors/probe")

	assert response.status_code == 503


@pytest.mark.anyio
async def test_move_motor_success(client: AsyncClient) -> None:
	mock_worker = _mock_worker()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
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
async def test_move_fridge_door_delegates_tool_preparation_to_fridge(client: AsyncClient) -> None:
	fridge_door = _mock_motor(100.0)
	mock_worker = _mock_worker({"fridge_door": fridge_door})
	mock_worker._robot._fridge = MagicMock()
	mock_worker._robot._fridge.move_door_to = AsyncMock()
	mock_worker._robot._tool.is_fully_retracted.return_value = False

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/fridge_door/move",
				json={"position_mm": 150.0},
			)

	assert response.status_code == 200
	fridge_door.move_to_position.assert_not_awaited()
	mock_worker._robot._fridge.move_door_to.assert_awaited_once_with(150.0)
	mock_worker._robot._tool.is_fully_retracted.assert_not_awaited()


@pytest.mark.anyio
async def test_move_fridge_door_uses_fridge_abstraction(client: AsyncClient) -> None:
	fridge_door = _mock_motor(100.0)
	mock_worker = _mock_worker({"fridge_door": fridge_door})
	mock_worker._robot._fridge = MagicMock()
	mock_worker._robot._fridge.move_door_to = AsyncMock()

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/fridge_door/move",
				json={"position_mm": 150.0},
			)

	assert response.status_code == 200
	mock_worker._robot._fridge.move_door_to.assert_awaited_once_with(150.0)
	fridge_door.move_to_position.assert_not_awaited()


@pytest.mark.anyio
async def test_move_fridge_door_safety_rejection_is_non_fault_conflict(client: AsyncClient) -> None:
	fridge_door = _mock_motor(100.0)
	mock_worker = _mock_worker({"fridge_door": fridge_door})
	mock_worker._robot._fridge = MagicMock()
	mock_worker._robot._fridge.move_door_to = AsyncMock(
		side_effect=FridgeSafetyError("Tool must be fully retracted before moving fridge door")
	)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post(
				"/api/robot/debug/motors/fridge_door/move",
				json={"position_mm": 150.0},
			)

	assert response.status_code == 409
	assert response.json()["detail"]["code"] == "tool_not_retracted"
	fridge_door.move_to_position.assert_not_awaited()


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
async def test_move_motor_admitted_while_tray_on_tool(client: AsyncClient) -> None:
	"""An engineer has to be able to drive a held tray back to its cell by hand."""
	mock_worker = _mock_worker()
	mock_worker._tray_on_tool = True
	mock_worker.refresh_tray_on_tool = AsyncMock(return_value=True)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/x_axis/move", json={"position_mm": 250.5})

	assert response.status_code == 200
	mock_worker._robot._get_motor_by_name("x_axis").move_to_position.assert_awaited_once()


@pytest.mark.anyio
async def test_x_motion_guard_refusal_is_a_409_not_a_retry_503(client: AsyncClient) -> None:
	"""A rotated tool refuses X motion; telling the engineer to retry would repeat the refusal."""
	from openshelf_os.exceptions import RotationClearanceError

	refusal = RotationClearanceError("Tool is rotated to 20.00 deg; rotate it to the front or back position before moving X")
	motor = _mock_motor(100.0)
	motor.move_to_position = AsyncMock(side_effect=refusal)
	motor.home = AsyncMock(side_effect=refusal)
	mock_worker = _mock_worker({"x_axis": motor})

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			move = await client.post("/api/robot/debug/motors/x_axis/move", json={"position_mm": 10.0})
			home = await client.post("/api/robot/debug/motors/x_axis/home")

	for response in (move, home):
		assert response.status_code == 409
		assert response.json()["detail"]["code"] == "rotation_blocked"


@pytest.mark.anyio
async def test_home_fridge_door_uses_home_and_close_sequence(client: AsyncClient) -> None:
	fridge_door = _mock_motor(100.0)
	mock_worker = _mock_worker({"fridge_door": fridge_door})
	mock_worker._robot._fridge = MagicMock()
	mock_worker._robot._fridge.home_door = AsyncMock()
	mock_worker._robot._tool.is_fully_retracted.return_value = False

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/fridge_door/home")

	assert response.status_code == 200
	mock_worker._robot._fridge.home_door.assert_awaited_once()
	fridge_door.home.assert_not_awaited()
	mock_worker._robot._tool.is_fully_retracted.assert_not_awaited()
	mock_worker.clear_needs_home_axes.assert_called_once_with(["fridge_door"])


@pytest.mark.anyio
async def test_home_fridge_door_center_failure_marks_only_fridge_door_needing_home(
	client: AsyncClient,
) -> None:
	fridge_door = _mock_motor(100.0)
	mock_worker = _mock_worker({"fridge_door": fridge_door})
	mock_worker.set_needs_home = MagicMock()
	mock_worker._robot._fridge = MagicMock()
	mock_worker._robot._fridge.home_door = AsyncMock(
		side_effect=MotorError("Failed to home fridge door: center move failed")
	)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/fridge_door/home")

	assert response.status_code == 503
	assert response.json()["detail"]["code"] == "motor_home_failed"
	mock_worker.set_needs_home.assert_called_once_with(
		"Motor home failed: Failed to home fridge door: center move failed",
		axes=["fridge_door"],
	)
	mock_worker.get_status_snapshot.assert_awaited_once_with(force_refresh=True)
	mock_worker.clear_needs_home_axes.assert_not_called()


@pytest.mark.anyio
async def test_home_fridge_door_safety_rejection_is_non_fault_conflict(
	client: AsyncClient,
) -> None:
	fridge_door = _mock_motor(100.0)
	mock_worker = _mock_worker({"fridge_door": fridge_door})
	mock_worker.set_needs_home = MagicMock()
	mock_worker._robot._fridge = MagicMock()
	mock_worker._robot._fridge.home_door = AsyncMock(
		side_effect=FridgeSafetyError(
			"Tool must be fully retracted before moving fridge door"
		)
	)

	with patch("openshelf_os_server.routers.robot.get_settings", return_value=_mock_settings()):
		with patch("openshelf_os_server.routers.robot.get_robot_worker", return_value=mock_worker):
			response = await client.post("/api/robot/debug/motors/fridge_door/home")

	assert response.status_code == 409
	assert response.json()["detail"]["code"] == "tool_not_retracted"
	mock_worker.set_needs_home.assert_not_called()
	mock_worker.clear_needs_home_axes.assert_not_called()
