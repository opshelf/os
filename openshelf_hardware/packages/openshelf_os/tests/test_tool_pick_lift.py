from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from openshelf_os.common import AxisCoordinate
from openshelf_os.tool import Tool


@pytest.mark.asyncio
async def test_pick_lift_place_keeps_z_extended_until_after_release() -> None:
    events: list[str] = []
    coordinate = AxisCoordinate(x=123.0, y=456.0, z_offset=7.5, s=0)
    tool = Tool.__new__(Tool)
    tool._gs = SimpleNamespace(
        safe_open=AsyncMock(side_effect=lambda: events.append("safe_open")),
        open=AsyncMock(side_effect=lambda: events.append("grip")),
        close=AsyncMock(side_effect=lambda: events.append("release")),
    )
    tool._my = SimpleNamespace(
        move_to_position=AsyncMock(side_effect=lambda y: events.append(f"y:{y}")),
    )
    tool.move = AsyncMock(side_effect=lambda _: events.append("move"))
    tool.rotate_to_position = AsyncMock(side_effect=lambda _: events.append("rotate"))
    tool.extend_empty = AsyncMock(side_effect=lambda _: events.append("extend"))
    tool.retract_empty = AsyncMock(side_effect=lambda: events.append("retract"))
    tool._handle_fault = AsyncMock()
    sleep = AsyncMock(side_effect=lambda seconds: events.append(f"hold:{seconds}"))

    with patch("openshelf_os.tool.asyncio.sleep", sleep):
        await tool.pick_lift_place(coordinate, lift_mm=12.5, hold_seconds=1.0)

    assert events == [
        "safe_open",
        "move",
        "rotate",
        "extend",
        "grip",
        "y:468.5",
        "hold:1.0",
        "y:456.0",
        "release",
        "retract",
    ]
    tool._handle_fault.assert_not_awaited()
