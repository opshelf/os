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
		_mg=SimpleNamespace(move_to_position=AsyncMock()),
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
