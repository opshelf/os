from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from openshelf_os.laser_calibration.angle import (
	probe_stage_until_stall,
)
from openshelf_os.laser_calibration.angle.stage_stall import (
	resolve_stage_retract_target,
	retract_stage_after_stall,
)


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


