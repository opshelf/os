from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openshelf_os.exceptions import ConfigError, MovementError
from openshelf_os.tool import (
    ROTATION_HOME_SAFE_X_MM,
    RotationHomeOrientation,
    Tool,
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
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 184.0,
        'ROTATION_BACK': 4.0,
    }[name])
    tool._handle_fault = AsyncMock()

    await tool.home()

    rotate_motor.home.assert_awaited_once()
    rotate_motor.move_to_position.assert_awaited_once_with(4.0)


@pytest.mark.asyncio
async def test_home_restores_y_brake_before_homing_y_axis() -> None:
    events: list[str] = []

    class FakeYMotor:
        async def restore_brake_output(self) -> None:
            events.append("restore")

        async def home(self) -> None:
            events.append("home")

    y_motor = FakeYMotor()

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mg = None
    tool._mr = None
    tool._mx = None
    tool._my = y_motor
    tool._handle_fault = AsyncMock()

    await tool.home()

    assert events == ["restore", "home"]

@pytest.mark.asyncio
async def test_rotate_to_home_orientation_marks_error_when_move_fails() -> None:
    rotate_motor = AsyncMock()
    rotate_motor.move_to_position = AsyncMock(side_effect=RuntimeError("boom"))

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mr = rotate_motor
    tool._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])

    with pytest.raises(MovementError, match="Tool rotation to home orientation failed"):
        await tool.rotate_to_home_orientation()


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


def _mock_x_motor(
    limit: float = 600.0, lower_limit: float = 0.0, position: float = 100.0
) -> AsyncMock:
    motor = AsyncMock()
    motor._params = SimpleNamespace(lim=limit, lower_lim=lower_limit)
    motor.get_position = AsyncMock(return_value=position)
    motor.move_to_position = AsyncMock()
    return motor


@pytest.mark.asyncio
async def test_ensure_safe_rotation_x_position_moves_from_lower_edge() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=25.0)

    original_x = await tool.ensure_safe_rotation_x_position()

    assert original_x == pytest.approx(25.0)
    tool._mx.move_to_position.assert_awaited_once_with(60.0)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_x_position_moves_from_upper_edge() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=575.0)

    original_x = await tool.ensure_safe_rotation_x_position()

    assert original_x == pytest.approx(575.0)
    tool._mx.move_to_position.assert_awaited_once_with(540.0)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_x_position_skips_when_position_is_safe() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=100.0)

    original_x = await tool.ensure_safe_rotation_x_position()

    assert original_x is None
    tool._mx.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_safe_rotation_x_position_raises_when_x_axis_is_too_small() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(limit=100.0, position=25.0)

    with pytest.raises(ConfigError, match="X axis limit is too small"):
        await tool.ensure_safe_rotation_x_position()


@pytest.mark.asyncio
async def test_rotate_to_position_restores_original_x_after_safe_edge_move() -> None:
    events: list[str] = []

    x_motor = _mock_x_motor(position=25.0)
    x_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f"x_move:{position}"))

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=0.0)
    rotate_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f"rotate:{position}"))

    tool = Tool.__new__(Tool)
    tool._ms = None
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])

    await tool.rotate_to_position("front")

    assert events == [
        "x_move:60.0",
        "rotate:4.0",
        "x_move:25.0",
    ]


@pytest.mark.asyncio
async def test_rotate_to_position_skips_safety_when_angle_is_already_current() -> None:
    x_motor = _mock_x_motor(position=25.0)

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=4.0)
    rotate_motor.move_to_position = AsyncMock()

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor._params.rotation_safe_stage_extension_enabled = True

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])

    await tool.rotate_to_position("front")

    x_motor.move_to_position.assert_not_awaited()
    stage_motor.move_to_position.assert_not_awaited()
    rotate_motor.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_rotate_to_position_skips_safety_when_angle_is_within_position_tolerance() -> None:
    x_motor = _mock_x_motor(position=25.0)

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=4.03)
    rotate_motor.move_to_position = AsyncMock()

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor._params.rotation_safe_stage_extension_enabled = True

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 4.0,
        'ROTATION_BACK': 184.0,
    }[name])

    await tool.rotate_to_position("front")

    x_motor.move_to_position.assert_not_awaited()
    stage_motor.move_to_position.assert_not_awaited()
    rotate_motor.move_to_position.assert_not_awaited()


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
    x_motor._params = SimpleNamespace(lim=600.0, lower_lim=0.0)
    x_motor.get_position = AsyncMock(return_value=100.0)

    y_motor = AsyncMock()
    y_motor.home = AsyncMock(side_effect=lambda: events.append('y_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._my = y_motor
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
    assert rotate_motor.get_position.await_count == 2


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
    x_motor._params = SimpleNamespace(lim=600.0, lower_lim=0.0)
    x_motor.get_position = AsyncMock(return_value=100.0)

    y_motor = AsyncMock()
    y_motor.home = AsyncMock(side_effect=lambda: events.append('y_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._my = y_motor
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


@pytest.mark.asyncio
async def test_home_moves_x_from_edge_before_rotate_home_when_rotation_is_at_front() -> None:
    events: list[str] = []

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor.home = AsyncMock(side_effect=lambda: events.append('stage_home'))

    z_motor = AsyncMock()
    z_motor.home = AsyncMock(side_effect=lambda: events.append('z_home'))

    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=0.0)
    rotate_motor.home = AsyncMock(side_effect=lambda: events.append('rotate_home'))
    rotate_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f'rotate_move:{position}'))

    x_motor = AsyncMock()
    x_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f'x_move:{position}'))
    x_motor.home = AsyncMock(side_effect=lambda: events.append('x_home'))
    x_motor._params = SimpleNamespace(lim=600.0, lower_lim=0.0)
    x_motor.get_position = AsyncMock(side_effect=[25.0, 60.0])

    y_motor = AsyncMock()
    y_motor.home = AsyncMock(side_effect=lambda: events.append('y_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = x_motor
    tool._my = y_motor
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
        'x_move:60.0',
        'rotate_home',
        'x_move:25.0',
        'x_home',
        'y_home',
    ]
