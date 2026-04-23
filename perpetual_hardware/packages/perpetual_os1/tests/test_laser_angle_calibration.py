import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from perpetual_os1.laser_angle_calibration import (
	RIGHT_MODULE_ROTATION_SAFE_X_MM,
	ShelfSurfaceEdge,
	filter_shelf_edges,
	get_default_rotation_center_deg,
	prepare_capture_pose,
	probe_stage_until_stall,
	run_with_temp_robot_config,
)


def test_get_default_rotation_center_deg_uses_front_for_left() -> None:
	assert get_default_rotation_center_deg('left') == 0.0


def test_get_default_rotation_center_deg_uses_back_for_right() -> None:
	assert get_default_rotation_center_deg('right') == 180.0


def test_filter_shelf_edges_keeps_only_detected_edges() -> None:
	filtered_edges = filter_shelf_edges(
		edges=[],
		y_min_mm=0.0,
		y_max_mm=100.0,
		side_name='left',
		scout_x_positions=[25.0],
	)

	assert filtered_edges == []


def test_filter_shelf_edges_discards_close_duplicates_without_appending_floor() -> None:
	filtered_edges = filter_shelf_edges(
		edges=[
			ShelfSurfaceEdge(x_mm=25.0, y_mm=80.0),
			ShelfSurfaceEdge(x_mm=25.0, y_mm=48.0),
			ShelfSurfaceEdge(x_mm=25.0, y_mm=35.0),
		],
		y_min_mm=0.0,
		y_max_mm=100.0,
		side_name='left',
		scout_x_positions=[25.0],
	)

	assert filtered_edges == [
		ShelfSurfaceEdge(x_mm=25.0, y_mm=80.0),
		ShelfSurfaceEdge(x_mm=25.0, y_mm=35.0),
	]


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
	assert tool._mr.move_to_position.await_args_list[0].args == (180.0,)


@pytest.mark.asyncio
async def test_probe_stage_until_stall_can_skip_auto_retract() -> None:
	stage_motor = SimpleNamespace(
		move_to_position=AsyncMock(),
		move_to_position_no_wait=AsyncMock(),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
		get_position=AsyncMock(side_effect=[40.0, 40.5, 40.5, 40.5]),
		get_param=AsyncMock(return_value=0),
	)
	tool = SimpleNamespace(_ms=stage_motor)

	did_stall, stage_position_mm, status, alarm_code = await probe_stage_until_stall(
		tool,
		target_stage_mm=50.0,
		retract_stage_mm=3.0,
		settle_ms=0,
		timeout_s=1.0,
		probe_rpm=50,
		no_progress_s=0.0,
		probe_step_mm=1.0,
		fast_approach_mm=40.0,
		fast_approach_rpm=200,
		auto_retract=False,
	)

	assert did_stall is True
	assert stage_position_mm == pytest.approx(40.5)
	assert status == 'no_progress'
	assert alarm_code == 0
	assert stage_motor.move_to_position.await_count == 2
	assert stage_motor.move_to_position.await_args_list[0].args == (3.0,)
	assert stage_motor.move_to_position.await_args_list[1].args == (40.0,)


@pytest.mark.asyncio
async def test_run_with_temp_robot_config_skips_stop_when_cancelled(tmp_path) -> None:
	robot = SimpleNamespace(
		configure=AsyncMock(),
		stop=AsyncMock(),
		shutdown=AsyncMock(),
	)
	started = asyncio.Event()

	async def did_run(current_robot) -> None:
		assert current_robot is robot
		started.set()
		await asyncio.sleep(10)

	with patch('perpetual_os1.laser_angle_calibration.build_standalone_config', return_value={}):
		with patch('perpetual_os1.robot.Robot', return_value=robot):
			task = asyncio.create_task(
				run_with_temp_robot_config(
					config=SimpleNamespace(),
					config_path=str(tmp_path / 'config.json'),
					did_run=did_run,
					shutdown_action='stop',
				)
			)

			await started.wait()
			task.cancel()

			with pytest.raises(asyncio.CancelledError):
				await task

	robot.stop.assert_not_awaited()
	robot.shutdown.assert_not_awaited()
