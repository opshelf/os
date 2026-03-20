from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient


def _mock_worker_for_move():
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

	calibration_point = SimpleNamespace(x=120.0, y=340.0, z_offset=2.0)
	cell = SimpleNamespace(calibration_point=calibration_point)
	shelf = SimpleNamespace(cells=[cell])
	grid = MagicMock()
	grid.get_side = MagicMock(return_value=[shelf])

	robot = MagicMock()
	robot._grid = grid
	robot._tool = tool

	worker = AsyncMock()
	worker._robot = robot

	return worker, tool, order


@pytest.mark.anyio
@pytest.mark.parametrize(
	("side", "expected_rotation", "expected_slide"),
	[("left_module", "front", 40.0), ("right_module", "back", 45.0)],
)
async def test_move_to_cell_rotates_before_z_extension(
	client: AsyncClient,
	side: str,
	expected_rotation: str,
	expected_slide: float,
) -> None:
	worker, tool, order = _mock_worker_for_move()
	clear_motors = AsyncMock()

	with patch("perpetual_os1_server.routers.calibration.get_robot_worker", return_value=worker):
		with patch("perpetual_os1_server.routers.calibration._clear_motors_for_move", new=clear_motors):
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
