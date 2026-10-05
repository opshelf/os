from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openshelf_os.exceptions import ConfigError, MovementError
from openshelf_os.tool import (
    Tool,
)


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
    tool._mx.move_to_position.assert_awaited_once_with(200.0)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_x_position_moves_from_upper_edge() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=575.0)

    original_x = await tool.ensure_safe_rotation_x_position()

    assert original_x == pytest.approx(575.0)
    tool._mx.move_to_position.assert_awaited_once_with(400.0)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_x_position_skips_when_position_is_safe() -> None:
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=200.0)

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
        "x_move:200.0",
        "rotate:4.0",
        "x_move:25.0",
    ]


@pytest.mark.asyncio
async def test_rotate_to_position_refuses_unsafe_stage_clearance() -> None:
    stage_motor = _mock_stage_motor(position=3.0)
    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=2.0)
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = stage_motor
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])

    with pytest.raises(MovementError, match="stage=3.000mm exceeds maximum 0.500mm"):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


@pytest.mark.asyncio
async def test_rotate_to_position_refuses_unsafe_z_clearance() -> None:
    stage_motor = _mock_stage_motor(position=0.0)
    z_motor = AsyncMock()
    z_motor.get_position = AsyncMock(return_value=1.0)
    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=2.0)
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._mr = rotate_motor
    tool._mx = None
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])

    with pytest.raises(MovementError, match="z_axis=1.000mm exceeds maximum 0.500mm"):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


def _tool_for_x_lim_safe_check(
    *, x_lim_minunsafe: bool | None = True, x_lim_maxunsafe: bool | None = True
) -> tuple[Tool, AsyncMock]:
    rotate_motor = AsyncMock()
    rotate_motor.get_position = AsyncMock(return_value=2.0)
    rotate_motor.move_to_position = AsyncMock()

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = None
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])
    tool._x_lim_minunsafe_input = (
        None if x_lim_minunsafe is None else SimpleNamespace(read=lambda: x_lim_minunsafe)
    )
    tool._x_lim_maxunsafe_input = (
        None if x_lim_maxunsafe is None else SimpleNamespace(read=lambda: x_lim_maxunsafe)
    )

    return tool, rotate_motor


@pytest.mark.asyncio
@pytest.mark.parametrize(
	"kwargs, error",
	[
		pytest.param(
			{"x_lim_minunsafe": False},
			"x_lim_minunsafe is not engaged",
			id="min_unsafe",
		),
		pytest.param(
			{"x_lim_maxunsafe": False},
			"x_lim_maxunsafe is not engaged",
			id="max_unsafe",
		),
	],
)
async def test_rotate_to_position_refuses_when_x_lim_not_engaged(
	kwargs: dict, error: str
) -> None:
    tool, rotate_motor = _tool_for_x_lim_safe_check(**kwargs)

    with pytest.raises(MovementError, match=error):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


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
    z_motor.get_position = AsyncMock(return_value=0.0)

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
    z_motor.get_position = AsyncMock(return_value=0.0)

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
async def test_retract_homes_stage_when_zero_move_stops_outside_clearance() -> None:
    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor.get_position = AsyncMock(side_effect=[0.75, 0.0])
    stage_motor.clear_alarm = AsyncMock()
    stage_motor.home = AsyncMock()
    stage_motor.idle = AsyncMock()
    z_motor = AsyncMock()
    z_motor.move_to_position = AsyncMock()
    z_motor.get_position = AsyncMock(return_value=0.0)

    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(parameters=SimpleNamespace(
        STAGE_RETRACT_POSITION_MM=0.0,
        Z_RETRACT_POSITION_MM=0.0,
        ROTATION_STAGE_MAX_EXTENSION_MM=0.5,
        ROTATION_Z_MAX_EXTENSION_MM=0.5,
    ))
    tool._ms = stage_motor
    tool._mg = z_motor
    tool._handle_fault = AsyncMock()
    tool._logger = Mock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'Z_MOVE_RPM': 100,
        'Z_MOVE_ACCEL': 200,
    }[name])

    await tool.retract()

    stage_motor.clear_alarm.assert_awaited_once()
    stage_motor.home.assert_awaited_once()
    stage_motor.idle.assert_awaited_once()


