from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from perpetual_os1.exceptions import ConfigError
from perpetual_os1.tool import ROTATION_SAFE_STAGE_EXTENSION_MM, Tool, ToolItemStatus


def _mock_stage_motor(limit: float = 50.0, position: float = 0.0) -> AsyncMock:
    motor = AsyncMock()
    motor._params = SimpleNamespace(lim=limit)
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
    tool._ms.move_to_position.assert_awaited_once_with(ROTATION_SAFE_STAGE_EXTENSION_MM)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_stage_extension_raises_when_limit_too_small() -> None:
    tool = Tool.__new__(Tool)
    tool._ms = _mock_stage_motor(limit=5.0, position=0.0)
    tool._ms._params.rotation_safe_stage_extension_enabled = True

    with pytest.raises(ConfigError, match="ROTATION_SAFE_STAGE_EXTENSION_MM exceeds stage limit"):
        await tool.ensure_safe_rotation_stage_extension()


@pytest.mark.asyncio
async def test_home_retracts_stage_after_safe_rotation_extension() -> None:
    events: list[str] = []

    stage_motor = _mock_stage_motor(position=0.0)
    stage_motor._params.rotation_safe_stage_extension_enabled = True
    stage_motor.home = AsyncMock(side_effect=lambda: events.append('stage_home'))
    stage_motor.move_to_position = AsyncMock(side_effect=lambda position: events.append(f'stage_move:{position}'))

    rotate_motor = AsyncMock()
    rotate_motor.home = AsyncMock(side_effect=lambda: events.append('rotate_home'))

    tool = Tool.__new__(Tool)
    tool._ms = stage_motor
    tool._mg = None
    tool._mr = rotate_motor
    tool._mx = None
    tool._my = None
    tool._istatus = ToolItemStatus.EMPTY
    tool._handle_fault = AsyncMock()

    await tool.home()

    assert events == [
        'stage_home',
        f'stage_move:{ROTATION_SAFE_STAGE_EXTENSION_MM}',
        'rotate_home',
        'stage_home',
    ]
    assert stage_motor.home.await_count == 2
