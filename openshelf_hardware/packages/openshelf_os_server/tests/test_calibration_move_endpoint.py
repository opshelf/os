from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from openshelf_os.exceptions import MotorError, MovementError


def _mock_worker_for_move(stage_position: float = 0.0):
	order: list[str] = []

	async def did_move(*args, **kwargs):
		order.append("move")

	async def did_rotate(*args, **kwargs):
		order.append("rotate")

	async def did_move_z(*args, **kwargs):
		order.append("z")

	async def did_get_param(param: str):
		if param == "Z_EXTENSION_PLACE":
			return 10.0
		if param == "Z_MOVE_RPM":
			return 100
		if param == "Z_MOVE_ACCEL":
			return 1000
		if param == "STAGE_EXTENSION_SHELF_FRONT":
			return 40.0
		if param == "STAGE_EXTENSION_SHELF_BACK":
			return 45.0
		return None

	tool = MagicMock()
	tool.move = AsyncMock(side_effect=did_move)
	tool.rotate_to_position = AsyncMock(side_effect=did_rotate)
	tool.get_param = AsyncMock(side_effect=did_get_param)
	tool._mg = MagicMock()
	tool._mg.move_to_position = AsyncMock(side_effect=did_move_z)
	tool._ms = MagicMock()
	tool._ms.get_position = AsyncMock(return_value=stage_position)

	calibration_point = SimpleNamespace(x=120.0, y=340.0, z_offset=2.0)
	cell = SimpleNamespace(calibration_point=calibration_point)
	shelf = MagicMock(cells=[cell])
	shelf.get_cell.return_value = cell
	grid = MagicMock()
	grid.get_shelf.return_value = shelf
	grid.get_side.return_value = [shelf]

	robot = MagicMock()
	robot._grid = grid
	robot._tool = tool

	worker = AsyncMock()
	worker._robot = robot
	worker.set_needs_home = MagicMock()
	worker.needs_home_axes = MagicMock(return_value=[])
	worker.get_status_snapshot = AsyncMock(return_value={})

	return worker, tool, order


@pytest.mark.anyio
@pytest.mark.parametrize(
	("side", "expected_rotation", "expected_slide"),
	[("front_module", "front", 40.0), ("back_module", "back", 45.0)],
)
async def test_move_to_cell_rotates_before_z_extension(
	client: AsyncClient,
	side: str,
	expected_rotation: str,
	expected_slide: float,
) -> None:
	worker, tool, order = _mock_worker_for_move(stage_position=expected_slide)
	clear_motors = AsyncMock()

	with patch("openshelf_os_server.routers.calibration.get_robot_worker", return_value=worker):
		with patch("openshelf_os_server.routers.calibration._clear_motors_for_move", new=clear_motors):
			response = await client.post(
				"/api/calibration/move",
				json={"side": side, "shelf_idx": 0, "cell_idx": 0},
			)

	assert response.status_code == 200
	assert order == ["move", "rotate", "z"]
	assert response.json()["slide_extension"] == pytest.approx(expected_slide)

	clear_motors.assert_awaited_once_with(
		tool,
		gantry=True,
		gripper=True,
		stage=True,
		rotation=True,
	)
	tool.rotate_to_position.assert_awaited_once_with(expected_rotation)


def _mock_worker_for_rotation() -> tuple[MagicMock, MagicMock]:
	worker = MagicMock()
	worker.needs_home.return_value = False
	worker.needs_home_axes.return_value = []
	worker.needs_home_reason.return_value = None
	worker.set_needs_home = MagicMock()
	worker.get_status_snapshot = AsyncMock(return_value={})
	worker._current_command = None
	worker._demo_cycling = False
	worker._safety_guard_bypassed = False
	worker._safety_motion_blocked = False
	worker._awaiting_safety_ack = False
	worker._safety_active_sources = []
	worker._command_queue = SimpleNamespace(qsize=lambda: 0)
	worker._queued_commands = []
	worker._current_operation = None

	tool = MagicMock()
	tool._mx = MagicMock(_motor_id=11)
	tool._mx.clear_alarm = AsyncMock()
	tool._my = MagicMock(_motor_id=12)
	tool._my.clear_alarm = AsyncMock()
	tool._ms = MagicMock(_motor_id=13)
	tool._ms.clear_alarm = AsyncMock()
	tool._mr = MagicMock(_motor_id=14)
	tool._mr._params = SimpleNamespace(lim=180.0)
	tool._mr.clear_alarm = AsyncMock()
	tool._mr.move_to_position = AsyncMock()
	tool.is_rotation_at_position = AsyncMock(return_value=False)
	tool.ensure_safe_rotation_x_position = AsyncMock(return_value=5.0)
	tool.ensure_safe_rotation_stage_extension = AsyncMock()
	tool.restore_safe_rotation_x_position = AsyncMock()

	worker._robot = MagicMock()
	worker._robot._tool = tool
	return worker, tool


@pytest.mark.anyio
async def test_rotate_to_angle_marks_failed_safe_axis(client: AsyncClient) -> None:
	worker, tool = _mock_worker_for_rotation()
	worker.needs_home_axes.return_value = ["x_axis"]
	tool.restore_safe_rotation_x_position.side_effect = MotorError(
		"Movement failed for motor 11 (status=FAULT)"
	)

	with patch("openshelf_os_server.routers.calibration.get_robot_worker", return_value=worker):
		with patch("openshelf_os_server.routers.calibration.ensure_laser_auto_calibration_not_running", new=AsyncMock()):
			response = await client.post("/api/calibration/rotation/rotate?angle=42")

	assert response.status_code == 409
	worker.set_needs_home.assert_called_once()
	assert worker.set_needs_home.call_args.kwargs["axes"] == ["x_axis"]
	assert response.json()["detail"]["needs_home_axes"] == ["x_axis"]


@pytest.mark.anyio
async def test_rotate_to_angle_clearance_blocked_returns_409(client: AsyncClient) -> None:
	worker, tool = _mock_worker_for_rotation()
	tool.ensure_safe_rotation_stage_extension.side_effect = MovementError(
		"Unsafe tool clearance for rotation: z_axis=162.470mm exceeds maximum 5.000mm"
	)

	with patch("openshelf_os_server.routers.calibration.get_robot_worker", return_value=worker):
		with patch("openshelf_os_server.routers.calibration.ensure_laser_auto_calibration_not_running", new=AsyncMock()):
			response = await client.post("/api/calibration/rotation/rotate?angle=42")

	assert response.status_code == 409
	detail = response.json()["detail"]
	assert detail["code"] == "rotation_blocked"
	assert "z_axis=162.470mm exceeds maximum 5.000mm" in detail["message"]
	assert "needs_home" not in detail
	worker.set_needs_home.assert_not_called()
