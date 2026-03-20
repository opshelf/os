from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from perpetual_os1.exceptions import ConfigError
from perpetual_os1.tool import ROTATION_SAFE_STAGE_EXTENSION_MM, Tool


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

    await tool.ensure_safe_rotation_stage_extension()

    tool._ms.get_position.assert_not_awaited()
    tool._ms.move_to_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_safe_rotation_stage_extension_moves_when_enabled() -> None:
    tool = Tool.__new__(Tool)
    tool._ms = _mock_stage_motor(position=0.0)
    tool._ms._params.rotation_safe_stage_extension_enabled = True

    await tool.ensure_safe_rotation_stage_extension()

    tool._ms.move_to_position.assert_awaited_once_with(ROTATION_SAFE_STAGE_EXTENSION_MM)


@pytest.mark.asyncio
async def test_ensure_safe_rotation_stage_extension_raises_when_limit_too_small() -> None:
    tool = Tool.__new__(Tool)
    tool._ms = _mock_stage_motor(limit=5.0, position=0.0)
    tool._ms._params.rotation_safe_stage_extension_enabled = True

    with pytest.raises(ConfigError, match="ROTATION_SAFE_STAGE_EXTENSION_MM exceeds stage limit"):
        await tool.ensure_safe_rotation_stage_extension()
