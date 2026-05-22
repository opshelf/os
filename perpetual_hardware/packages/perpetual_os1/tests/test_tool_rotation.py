from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from perpetual_os1.exceptions import ConfigError, MovementError
from perpetual_os1.tool import (
    ROTATION_HOME_SAFE_X_MM,
    RotationHomeOrientation,
    Tool,
    ToolItemStatus,
    ToolMotionStatus,
    derive_rotation_home_orientation,
)


def test_derive_rotation_home_orientation_detects_left_module() -> None:
    orientation = derive_rotation_home_orientation(rotation_front=4.0, rotation_back=184.0)

    assert orientation == RotationHomeOrientation.LEFT_MODULE


def test_derive_rotation_home_orientation_detects_right_module() -> None:
    orientation = derive_rotation_home_orientation(rotation_front=184.0, rotation_back=4.0)

    assert orientation == RotationHomeOrientation.RIGHT_MODULE


def test_derive_rotation_home_orientation_rejects_ambiguous_angles() -> None:
    with pytest.raises(ConfigError, match="equally close to home"):
        derive_rotation_home_orientation(rotation_front=4.0, rotation_back=-4.0)


@pytest.mark.asyncio
async def test_home_moves_rotate_to_front_after_homing_for_left_facing_device() -> None:
    rotate_motor = AsyncMock()
    rotate_motor.home = AsyncMock()
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._my = None
    tool._istatus = ToolItemStatus.EMPTY
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])
    tool._handle_fault = AsyncMock()

    await tool.home()

    rotate_motor.home.assert_awaited_once()
    rotate_motor.move_to_position.assert_awaited_once_with(4.0)


@pytest.mark.asyncio
async def test_home_moves_rotate_to_back_after_homing_for_right_facing_device() -> None:
    rotate_motor = AsyncMock()
    rotate_motor.home = AsyncMock()
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._my = None
    tool._istatus = ToolItemStatus.EMPTY
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 184.0,
        'ROTATION_BACK': 4.0,
    }[name])
    tool._handle_fault = AsyncMock()

    await tool.home()

    rotate_motor.home.assert_awaited_once()
    rotate_motor.move_to_position.assert_awaited_once_with(4.0)


@pytest.mark.asyncio
async def test_rotate_to_home_orientation_marks_error_when_move_fails() -> None:
    rotate_motor = AsyncMock()
    rotate_motor.move_to_position = AsyncMock(side_effect=RuntimeError("boom"))

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mr = rotate_motor
    tool._istatus = ToolItemStatus.EMPTY
    tool._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])

    with pytest.raises(MovementError, match="Tool rotation to home orientation failed"):
        await tool.rotate_to_home_orientation()

    assert tool._mstatus == ToolMotionStatus.ERROR
    assert tool._status.motion == ToolMotionStatus.ERROR

def _mock_stage_motor(
    limit: float = 50.0, position: float = 0.0, safe_extension_mm: float = 8.5
) -> AsyncMock:
    motor = AsyncMock()
    motor._params = SimpleNamespace(
        lim=limit, rotation_safe_stage_extension_mm=safe_extension_mm
    )
    motor.get_position = AsyncMock(return_value=position)
    motor.move_to_position = AsyncMock()
    return motor


@pytest.mark.asyncio
async def test_ensure_safe_rotation_stage_extension_skips_when_disabled() -> None:
    tool = Tool.__new__(Tool)
    tool._ms = _mock_stage_motor(position=0.0)

    did_extend = await tool.ensure_safe_rotation_stage_extension()

    assert did_extend is False
    tool._ms.get_position.assert_not_awaited()
    tool._ms.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_safe_rotation_stage_extension_moves_when_enabled() -> None:
    tool = Tool.__new__(Tool)
    tool._ms = _mock_stage_motor(position=0.0)
    tool._ms._params.rotation_safe_stage_extension_enabled = True

    did_extend = await tool.ensure_safe_rotation_stage_extension()

    assert did_extend is True
    tool._ms.move_to_position.assert_awaited_once_with(8.5)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_stage_extension_raises_when_limit_too_small() -> None:
    tool = Tool.__new__(Tool)
    tool._ms = _mock_stage_motor(limit=5.0, position=0.0, safe_extension_mm=8.5)
    tool._ms._params.rotation_safe_stage_extension_enabled = True

    with pytest.raises(ConfigError, match="rotation_safe_stage_extension_mm exceeds stage limit"):
        await tool.ensure_safe_rotation_stage_extension()

@pytest.mark.asyncio
async def test_retract_homes_stage_when_stage_retract_faults() -> None:
    stage_error = RuntimeError('stage fault')
    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor.clear_alarm = AsyncMock()
    stage_motor.home = AsyncMock()
    stage_motor.idle = AsyncMock()
    stage_motor.move_to_position = AsyncMock(side_effect=stage_error)

    z_motor = AsyncMock()
    z_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mstatus = ToolMotionStatus.IDLE
    tool._istatus = ToolItemStatus.EMPTY
    tool._status = SimpleNamespace(motion=ToolMotionStatus.IDLE, item=ToolItemStatus.EMPTY)
    tool._handle_fault = AsyncMock()
    tool._logger = Mock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'Z_MOVE_RPM': 100.0,
        'Z_MOVE_ACCEL': 200.0,
    }[name])

    await tool.retract()

    z_motor.move_to_position.assert_awaited_once_with(1, rpm=100.0, accel=200.0)
    stage_motor.move_to_position.assert_awaited_once_with(3.0)
    stage_motor.clear_alarm.assert_awaited_once()
    stage_motor.home.assert_awaited_once()
    stage_motor.idle.assert_awaited_once()
    tool._handle_fault.assert_not_awaited()
    assert tool._mstatus == ToolMotionStatus.IDLE


@pytest.mark.asyncio
async def test_retract_fails_when_stage_home_recovery_leaves_stage_unsafe() -> None:
    stage_error = RuntimeError('stage fault')
    stage_motor = _mock_stage_motor(position=6.0)
    stage_motor.clear_alarm = AsyncMock()
    stage_motor.home = AsyncMock()
    stage_motor.idle = AsyncMock()
    stage_motor.move_to_position = AsyncMock(side_effect=stage_error)

    z_motor = AsyncMock()
    z_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mstatus = ToolMotionStatus.IDLE
    tool._istatus = ToolItemStatus.EMPTY
    tool._status = SimpleNamespace(motion=ToolMotionStatus.IDLE, item=ToolItemStatus.EMPTY)
    tool._handle_fault = AsyncMock()
    tool._logger = Mock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'Z_MOVE_RPM': 100.0,
        'Z_MOVE_ACCEL': 200.0,
    }[name])

    with pytest.raises(MovementError, match='Tool retraction failed'):
        await tool.retract()

    stage_motor.home.assert_awaited_once()
    stage_motor.idle.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()

@pytest.mark.asyncio
async def test_home_retracts_stage_after_safe_rotation_extension() -> None:
    events: list[str] = []

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor._params.rotation_safe_stage_extension_enabled = True
    stage_motor.home = AsyncMock(side_effect=lambda: events.append('stage_home'))
    stage_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f'stage_move:{position}'))

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=0.0)
    rotate_motor.home = AsyncMock(side_effect=lambda: events.append('rotate_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._my = None
    tool._istatus = ToolItemStatus.EMPTY
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 0.0,
        'ROTATION_BACK': 184.0,
    }[name])
    tool._handle_fault = AsyncMock()

    await tool.home()

    assert events == [
        'stage_home',
        'stage_move:8.5',
        'rotate_home',
        'stage_home',
    ]
    assert stage_motor.home.await_count == 2


@pytest.mark.asyncio
async def test_home_moves_x_before_rotate_home_when_rotation_is_past_front() -> None:
    events: list[str] = []

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor.home = AsyncMock(side_effect=lambda: events.append('stage_home'))

    z_motor = AsyncMock()
    z_motor.home = AsyncMock(side_effect=lambda: events.append('z_home'))

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=15.0)
    rotate_motor.home = AsyncMock(side_effect=lambda: events.append('rotate_home'))

    x_motor = AsyncMock()
    x_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f'x_move:{position}'))
    x_motor.home = AsyncMock(side_effect=lambda: events.append('x_home'))

    y_motor = AsyncMock()
    y_motor.home = AsyncMock(side_effect=lambda: events.append('y_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._my = y_motor
    tool._istatus = ToolItemStatus.EMPTY
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 0.0,
        'ROTATION_BACK': 184.0,
    }[name])
    tool._handle_fault = AsyncMock()
    tool.ensure_safe_rotation_stage_extension = AsyncMock(return_value=False)

    await tool.home()

    assert events == [
        'stage_home',
        'z_home',
        f'x_move:{ROTATION_HOME_SAFE_X_MM}',
        'rotate_home',
        'x_home',
        'y_home',
    ]
    tool.get_param.assert_any_await('ROTATION_FRONT')
    rotate_motor.get_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_home_does_not_move_x_before_rotate_home_when_rotation_is_at_front() -> None:
    events: list[str] = []

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor.home = AsyncMock(side_effect=lambda: events.append('stage_home'))

    z_motor = AsyncMock()
    z_motor.home = AsyncMock(side_effect=lambda: events.append('z_home'))

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=0.0)
    rotate_motor.home = AsyncMock(side_effect=lambda: events.append('rotate_home'))

    x_motor = AsyncMock()
    x_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f'x_move:{position}'))
    x_motor.home = AsyncMock(side_effect=lambda: events.append('x_home'))

    y_motor = AsyncMock()
    y_motor.home = AsyncMock(side_effect=lambda: events.append('y_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._my = y_motor
    tool._istatus = ToolItemStatus.EMPTY
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 0.0,
        'ROTATION_BACK': 184.0,
    }[name])
    tool._handle_fault = AsyncMock()
    tool.ensure_safe_rotation_stage_extension = AsyncMock(return_value=False)

    await tool.home()

    assert events == [
        'stage_home',
        'z_home',
        'rotate_home',
        'x_home',
        'y_home',
    ]
    x_motor.move_to_position.assert_not_awaited()
