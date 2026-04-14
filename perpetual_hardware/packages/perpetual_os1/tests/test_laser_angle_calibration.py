from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from perpetual_os1.laser_angle_calibration import (
	RIGHT_MODULE_ROTATION_SAFE_X_MM,
	get_default_rotation_center_deg,
	prepare_capture_pose,
)


def test_get_default_rotation_center_deg_uses_front_for_left() -> None:
	assert get_default_rotation_center_deg('left') == 0.0


def test_get_default_rotation_center_deg_uses_back_for_right() -> None:
	assert get_default_rotation_center_deg('right') == 180.0


@pytest.mark.asyncio
async def test_prepare_capture_pose_skips_rotation_move_for_right_when_rotation_is_unchanged() -> None:
	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_mr=SimpleNamespace(move_to_position=AsyncMock()),
		_ms=SimpleNamespace(ready=AsyncMock(), move_to_position=AsyncMock()),
		_mg=SimpleNamespace(move_to_position=AsyncMock()),
		gantry_wait=AsyncMock(),
		ensure_safe_rotation_stage_extension=AsyncMock(),
	)

	await prepare_capture_pose(
		tool,
		side_name='right',
		side_number=1,
		x_mm=845.0,
		y_mm=1096.0,
		absolute_rotation_deg=176.0,
		stage_extension_mm=13.0,
		z_mm=140.5,
		z_move_rpm=100.0,
		z_move_accel=100.0,
		should_rotate=False,
		should_use_safe_right_rotation_path=False,
		should_move_z=False,
	)

	tool.ensure_safe_rotation_stage_extension.assert_not_awaited()
	tool._mr.move_to_position.assert_not_awaited()
	tool._mx.move_to_position_no_wait.assert_awaited_once_with(845.0)
	tool._my.move_to_position_no_wait.assert_awaited_once_with(1096.0)


@pytest.mark.asyncio
async def test_prepare_capture_pose_rotates_in_place_for_right_when_rotation_changes() -> None:
	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_mr=SimpleNamespace(move_to_position=AsyncMock()),
		_ms=SimpleNamespace(ready=AsyncMock(), move_to_position=AsyncMock()),
		_mg=SimpleNamespace(move_to_position=AsyncMock()),
		gantry_wait=AsyncMock(),
		ensure_safe_rotation_stage_extension=AsyncMock(),
	)

	await prepare_capture_pose(
		tool,
		side_name='right',
		side_number=1,
		x_mm=945.0,
		y_mm=1096.0,
		absolute_rotation_deg=172.0,
		stage_extension_mm=13.0,
		z_mm=140.5,
		z_move_rpm=100.0,
		z_move_accel=100.0,
		should_rotate=True,
		should_use_safe_right_rotation_path=False,
		should_move_z=False,
	)

	tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	tool._mr.move_to_position.assert_awaited_once_with(172.0)
	tool._mx.move_to_position_no_wait.assert_awaited_once_with(945.0)
	tool._my.move_to_position_no_wait.assert_awaited_once_with(1096.0)


@pytest.mark.asyncio
async def test_prepare_capture_pose_uses_safe_right_rotation_path_when_requested() -> None:
	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_mr=SimpleNamespace(move_to_position=AsyncMock()),
		_ms=SimpleNamespace(ready=AsyncMock(), move_to_position=AsyncMock()),
		_mg=SimpleNamespace(move_to_position=AsyncMock()),
		gantry_wait=AsyncMock(),
		ensure_safe_rotation_stage_extension=AsyncMock(),
	)

	await prepare_capture_pose(
		tool,
		side_name='right',
		side_number=1,
		x_mm=945.0,
		y_mm=1096.0,
		absolute_rotation_deg=180.0,
		stage_extension_mm=13.0,
		z_mm=140.5,
		z_move_rpm=100.0,
		z_move_accel=100.0,
		should_rotate=True,
		should_use_safe_right_rotation_path=True,
		should_move_z=False,
	)

	tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	tool._mr.move_to_position.assert_awaited_once_with(180.0)
	assert tool._mx.move_to_position_no_wait.await_args_list[0].args == (RIGHT_MODULE_ROTATION_SAFE_X_MM,)
	assert tool._mx.move_to_position_no_wait.await_args_list[1].args == (945.0,)
