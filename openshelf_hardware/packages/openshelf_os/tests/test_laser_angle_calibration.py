import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import openshelf_os.laser_calibration.angle.cli as angle_cli
from openshelf_os.laser_calibration.angle import (
	RIGHT_MODULE_ROTATION_SAFE_X_MM,
	build_shelf_scout_x_positions,
	get_default_rotation_center_deg,
	prepare_capture_pose,
	resolve_rotation_center_deg,
	probe_stage_until_stall,
	run_with_temp_robot_config,
)
from openshelf_os.laser_calibration.angle.stage_stall import (
	finish_stage_stall_capture,
	resolve_stage_retract_target,
	retract_stage_after_stall,
)


def test_angle_cli_initializes_mode_workspace_before_parsing_args() -> None:
	args = SimpleNamespace()
	call_order: list[str] = []

	def parse_args() -> SimpleNamespace:
		call_order.append('parse_args')
		return args

	def validate_args(actual_args: SimpleNamespace) -> None:
		assert actual_args is args
		call_order.append('validate_args')

	async def run_capture(actual_args: SimpleNamespace) -> int:
		assert actual_args is args
		call_order.append('capture')
		return 0

	with patch.object(angle_cli, 'ensure_mode_workspace', side_effect=lambda: call_order.append('workspace')):
		with patch.object(angle_cli, 'parse_args', side_effect=parse_args):
			with patch.object(angle_cli, 'validate_args', side_effect=validate_args):
				with patch.object(angle_cli, 'run_stage_stall_async', side_effect=run_capture):
					assert angle_cli.main() == 0

	assert call_order == ['workspace', 'parse_args', 'validate_args', 'capture']


def test_get_default_rotation_center_deg_uses_front_for_left() -> None:
	assert get_default_rotation_center_deg('left') == 0.0


def test_get_default_rotation_center_deg_uses_back_for_right() -> None:
	assert get_default_rotation_center_deg('right') == 180.0


def test_resolve_rotation_center_deg_uses_configured_front_for_left() -> None:
	parameters = SimpleNamespace(ROTATION_FRONT=184.5, ROTATION_BACK=4.5)

	assert resolve_rotation_center_deg('left', parameters) == 184.5


def test_resolve_rotation_center_deg_uses_configured_back_for_right() -> None:
	parameters = SimpleNamespace(ROTATION_FRONT=184.5, ROTATION_BACK=4.5)

	assert resolve_rotation_center_deg('right', parameters) == 4.5




def test_angle_shelf_scout_uses_configured_scan_bounds_instead_of_probe_x() -> None:
	scan_config = SimpleNamespace(
		left_bounds_x_min_mm=0.0,
		left_bounds_x_max_mm=950.0,
	)

	assert build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=475.0,
		x_max_mm=475.0,
		side_name='left',
		scout_x_mm=None,
	) == [950.0]


def test_angle_shelf_scout_uses_configured_side_override() -> None:
	scan_config = SimpleNamespace(
		right_bounds_x_min_mm=0.0,
		right_bounds_x_max_mm=950.0,
		right_shelf_scout_x_mm=950.0,
	)

	assert build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=475.0,
		x_max_mm=475.0,
		side_name='right',
		scout_x_mm=None,
	) == [950.0]

def test_angle_shelf_scout_explicit_x_overrides_configured_scan_bounds() -> None:
	scan_config = SimpleNamespace(
		left_bounds_x_min_mm=0.0,
		left_bounds_x_max_mm=950.0,
	)

	assert build_shelf_scout_x_positions(
		scan_config=scan_config,
		x_min_mm=475.0,
		x_max_mm=475.0,
		side_name='left',
		scout_x_mm=475.0,
	) == [475.0]


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
		ensure_safe_rotation_x_position=AsyncMock(return_value=945.0),
		restore_safe_rotation_x_position=AsyncMock(),
		ensure_safe_rotation_stage_extension=AsyncMock(),
		is_rotation_at_position=AsyncMock(return_value=False),
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

	tool.ensure_safe_rotation_x_position.assert_awaited_once()
	tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	tool._mr.move_to_position.assert_awaited_once_with(172.0)
	tool.restore_safe_rotation_x_position.assert_awaited_once_with(945.0)
	tool._mx.move_to_position_no_wait.assert_awaited_once_with(945.0)
	tool._my.move_to_position_no_wait.assert_awaited_once_with(1096.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
	"pose",
	[
		pytest.param(
			(
				"right",
				1,
				945.0,
				1096.0,
				180.0,
			),
			id="right_module",
		),
		pytest.param(
			(
				"left",
				0,
				950.0,
				1250.0,
				0.0,
			),
			id="left_module",
		),
	],
)
async def test_prepare_capture_pose_uses_safe_rotation_path_when_requested(
	pose: tuple[str, int, float, float, float],
) -> None:
	side_name, side_number, x_mm, y_mm, rotation = pose
	tool = SimpleNamespace(
		_mx=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_my=SimpleNamespace(move_to_position_no_wait=AsyncMock()),
		_mr=SimpleNamespace(move_to_position=AsyncMock()),
		_ms=SimpleNamespace(ready=AsyncMock(), move_to_position=AsyncMock()),
		_mg=SimpleNamespace(move_to_position=AsyncMock()),
		gantry_wait=AsyncMock(),
		ensure_safe_rotation_x_position=AsyncMock(return_value=RIGHT_MODULE_ROTATION_SAFE_X_MM),
		restore_safe_rotation_x_position=AsyncMock(),
		ensure_safe_rotation_stage_extension=AsyncMock(),
		is_rotation_at_position=AsyncMock(return_value=False),
	)

	await prepare_capture_pose(
		tool,
		side_name=side_name,
		side_number=side_number,
		x_mm=x_mm,
		y_mm=y_mm,
		absolute_rotation_deg=rotation,
		stage_extension_mm=13.0,
		z_mm=140.5,
		z_move_rpm=100.0,
		z_move_accel=100.0,
		should_rotate=True,
		should_use_safe_right_rotation_path=True,
		should_move_z=False,
	)

	tool.ensure_safe_rotation_x_position.assert_awaited_once()
	tool.ensure_safe_rotation_stage_extension.assert_awaited_once()
	tool._mr.move_to_position.assert_awaited_once_with(rotation)
	tool.restore_safe_rotation_x_position.assert_awaited_once_with(RIGHT_MODULE_ROTATION_SAFE_X_MM)
	assert tool._mx.move_to_position_no_wait.await_args_list[0].args == (RIGHT_MODULE_ROTATION_SAFE_X_MM,)
	assert tool._mx.move_to_position_no_wait.await_args_list[1].args == (x_mm,)
	assert tool._mr.move_to_position.await_args_list[0].args == (rotation,)


@pytest.mark.asyncio
async def test_probe_stage_until_stall_accepts_small_target_lag() -> None:
	stage_position = 0.0
	target_position = 0.0

	async def did_move_no_wait(target_mm, **kwargs):
		nonlocal target_position, stage_position
		target_position = float(target_mm)
		stage_position = max(0.0, target_position - 0.25)

	stage_motor = SimpleNamespace(
		move_to_position=AsyncMock(),
		move_to_position_no_wait=AsyncMock(side_effect=did_move_no_wait),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
		get_position=AsyncMock(side_effect=lambda: stage_position),
		get_param=AsyncMock(return_value=0),
	)
	tool = SimpleNamespace(_ms=stage_motor)

	did_stall, stage_position_mm, status, alarm_code = await probe_stage_until_stall(
		tool,
		target_stage_mm=1.0,
		retract_stage_mm=0.0,
		settle_ms=0,
		timeout_s=1.0,
		probe_rpm=50,
		no_progress_s=0.2,
		probe_step_mm=0.5,
		fast_approach_mm=0.0,
		fast_approach_rpm=100,
		auto_retract=False,
	)

	assert did_stall is False
	assert status == 'limit'
	assert alarm_code is None
	assert stage_position_mm == pytest.approx(0.75)
	assert stage_motor.stop.await_count == 0


@pytest.mark.asyncio
async def test_probe_stage_until_stall_can_use_continuous_move() -> None:
	stage_position = 3.0

	async def did_move_no_wait(target_mm, **kwargs):
		nonlocal stage_position
		stage_position = 41.5

	stage_motor = SimpleNamespace(
		move_to_position=AsyncMock(),
		move_to_position_no_wait=AsyncMock(side_effect=did_move_no_wait),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
		get_position=AsyncMock(side_effect=lambda: stage_position),
		get_param=AsyncMock(return_value=32),
	)
	tool = SimpleNamespace(_ms=stage_motor)

	did_stall, stage_position_mm, status, alarm_code = await probe_stage_until_stall(
		tool,
		target_stage_mm=75.0,
		retract_stage_mm=3.0,
		settle_ms=0,
		timeout_s=1.0,
		probe_rpm=20,
		no_progress_s=0.2,
		probe_step_mm=0.5,
		fast_approach_mm=0.0,
		fast_approach_rpm=20,
		auto_retract=False,
		should_use_continuous_move=True,
	)

	assert did_stall is True
	assert stage_position_mm == pytest.approx(41.5)
	assert status == 'alarm'
	assert alarm_code == 32
	stage_motor.move_to_position_no_wait.assert_awaited_once_with(75.0, rpm=20)

@pytest.mark.asyncio
async def test_probe_stage_until_stall_detects_static_no_progress_after_command() -> None:
	stage_motor = SimpleNamespace(
		move_to_position=AsyncMock(),
		move_to_position_no_wait=AsyncMock(),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
		get_position=AsyncMock(side_effect=[39.27, 39.27, 39.27]),
		get_param=AsyncMock(return_value=0),
	)
	tool = SimpleNamespace(_ms=stage_motor)

	did_stall, stage_position_mm, status, alarm_code = await probe_stage_until_stall(
		tool,
		target_stage_mm=40.0,
		retract_stage_mm=39.5,
		settle_ms=0,
		timeout_s=1.0,
		probe_rpm=50,
		no_progress_s=0.0,
		probe_step_mm=0.5,
		fast_approach_mm=39.5,
		fast_approach_rpm=200,
		auto_retract=False,
	)

	assert did_stall is True
	assert stage_position_mm == pytest.approx(39.27)
	assert status == 'no_progress'
	assert alarm_code == 0
	assert stage_motor.stop.await_count == 1


@pytest.mark.asyncio
async def test_probe_stage_until_stall_can_skip_auto_retract() -> None:
	stage_motor = SimpleNamespace(
		move_to_position=AsyncMock(),
		move_to_position_no_wait=AsyncMock(),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
		get_position=AsyncMock(side_effect=[40.0, 40.1, 40.1, 40.1]),
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
	assert stage_position_mm == pytest.approx(40.1)
	assert status == 'no_progress'
	assert alarm_code == 0
	assert stage_motor.move_to_position.await_count == 2
	assert stage_motor.move_to_position.await_args_list[0].args == (3.0,)
	assert stage_motor.move_to_position.await_args_list[1].args == (40.0,)


@pytest.mark.asyncio
async def test_probe_stage_until_stall_reports_fast_approach_fault() -> None:
	stage_motor = SimpleNamespace(
		move_to_position=AsyncMock(side_effect=[None, RuntimeError('stage fault')]),
		move_to_position_no_wait=AsyncMock(),
		clear_alarm=AsyncMock(),
		stop=AsyncMock(),
		get_position=AsyncMock(return_value=42.0),
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
		fast_approach_mm=45.0,
		fast_approach_rpm=100,
		auto_retract=False,
	)

	assert did_stall is True
	assert stage_position_mm == pytest.approx(42.0)
	assert status == 'fast_approach_fault'
	assert alarm_code == 0
	stage_motor.move_to_position_no_wait.assert_not_awaited()

def test_resolve_stage_retract_target_backs_off_from_stall() -> None:
	assert resolve_stage_retract_target(
		did_stall=True,
		stage_position_mm=32.5,
		probe_start_mm=3.0,
		stage_backoff_mm=8.0,
	) == pytest.approx(24.5)

	assert resolve_stage_retract_target(
		did_stall=True,
		stage_position_mm=7.0,
		probe_start_mm=3.0,
		stage_backoff_mm=8.0,
	) == pytest.approx(3.0)


def test_resolve_stage_retract_target_backs_off_without_stall() -> None:
	assert resolve_stage_retract_target(
		did_stall=False,
		stage_position_mm=50.0,
		probe_start_mm=3.0,
		stage_backoff_mm=8.0,
	) == pytest.approx(42.0)


@pytest.mark.asyncio
async def test_retract_stage_after_stall_retries_after_fault() -> None:
	error = RuntimeError('stage fault')
	stage_motor = SimpleNamespace(
		clear_alarm=AsyncMock(),
		move_to_position=AsyncMock(side_effect=[error, None]),
	)
	tool = SimpleNamespace(_ms=stage_motor)

	await retract_stage_after_stall(
		tool,
		retract_stage_mm=3.0,
		probe_rpm=30,
		settle_s=0.0,
		max_attempts=2,
	)

	assert stage_motor.clear_alarm.await_count == 2
	assert stage_motor.move_to_position.await_count == 2
	stage_motor.move_to_position.assert_awaited_with(3.0, rpm=30)


@pytest.mark.asyncio
async def test_finish_stage_stall_capture_does_not_fully_retract_stage() -> None:
	z_motor = SimpleNamespace(move_to_position=AsyncMock())
	stage_motor = SimpleNamespace(idle=AsyncMock(), move_to_position=AsyncMock())
	tool = SimpleNamespace(_mg=z_motor, _ms=stage_motor)

	await finish_stage_stall_capture(tool, z_move_rpm=800.0, z_move_accel=1200.0)

	z_motor.move_to_position.assert_awaited_once_with(1, rpm=800.0, accel=1200.0)
	stage_motor.idle.assert_awaited_once_with()
	stage_motor.move_to_position.assert_not_awaited()

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

	with patch('openshelf_os.laser_calibration.angle.build_standalone_config', return_value={}):
		with patch('openshelf_os.laser_calibration.core.Robot', return_value=robot):
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
