from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openshelf_os.exceptions import MovementError
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
async def test_home_rotate_sequence_homes_unhomed_x_before_clearance_move() -> None:
    events: list[str] = []
    tool = Tool.__new__(Tool)
    tool._mx = _mock_x_motor(position=0.0)
    tool._mx._is_homed = False

    async def home_x() -> None:
        events.append("home_x")
        tool._mx._is_homed = True

    tool._mx.home = AsyncMock(side_effect=home_x)
    tool._mx.move_to_position = AsyncMock(side_effect=lambda mm: events.append(f"x:{mm}"))
    tool._mr = AsyncMock()
    tool._mr.get_position = AsyncMock(return_value=0.0)
    tool._mr.home = AsyncMock(side_effect=lambda: events.append("home_rotate"))
    tool._ms = None
    tool.get_param = AsyncMock(return_value=2.7)
    tool.ensure_safe_rotation_stage_extension = AsyncMock(return_value=False)
    tool.rotate_to_home_orientation = AsyncMock()

    await tool._home_rotate_sequence()

    assert events == ["home_x", "x:200.0", "home_rotate", "x:0.0"]


@pytest.mark.asyncio
async def test_rotate_to_position_restores_original_x_after_safe_edge_move() -> None:
    events: list[str] = []

    x_motor = _mock_x_motor(position=25.0)
    x_position = {"mm": 25.0}

    def didMoveX(position: float) -> None:
        events.append(f"x_move:{position}")
        x_position["mm"] = float(position)

    x_motor.move_to_position = AsyncMock(side_effect=didMoveX)
    x_motor.get_position = AsyncMock(side_effect=lambda: x_position["mm"])

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


def _tool_for_x_window_check(*, x_mm: float | None, is_homed: bool = True) -> tuple[Tool, AsyncMock]:
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
    tool._mx = None if x_mm is None else _mock_x_motor(limit=950.0, position=x_mm)
    if tool._mx is not None:
        tool._mx._is_homed = is_homed
        # The guard is under test, so keep the clearance move from correcting the position first.
        tool.ensure_safe_rotation_x_position = AsyncMock(return_value=None)
        tool.restore_safe_rotation_x_position = AsyncMock()
    tool._handle_fault = AsyncMock()
    tool.get_param = AsyncMock(side_effect=lambda name: {
        'ROTATION_FRONT': 182.25,
        'ROTATION_BACK': 2.0,
    }[name])

    return tool, rotate_motor


@pytest.mark.asyncio
@pytest.mark.parametrize(
	"x_mm, is_homed, error",
	[
		pytest.param(150.0, True, "outside the rotation window 200.0-750.0mm", id="near_min_end"),
		pytest.param(800.0, True, "outside the rotation window 200.0-750.0mm", id="near_max_end"),
		pytest.param(400.0, False, "x_axis is not homed", id="unhomed"),
	],
)
async def test_rotate_to_position_refuses_outside_the_x_window(x_mm: float, is_homed: bool, error: str) -> None:
    tool, rotate_motor = _tool_for_x_window_check(x_mm=x_mm, is_homed=is_homed)

    with pytest.raises(MovementError, match=error):
        await tool.rotate_to_position("front")

    rotate_motor.move_to_position.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


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
