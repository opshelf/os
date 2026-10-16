from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from openshelf_os.tool import Tool


def _make_tool() -> Tool:
    tool = Tool.__new__(Tool)
    tool._logger = logging.getLogger("test_init_isolation")
    tool._init_reporter = None
    tool._mx = AsyncMock()
    tool._my = AsyncMock()
    tool._mg = AsyncMock()
    tool._ms = AsyncMock()
    tool._mr = None
    return tool


def _record_home_order(tool: Tool) -> list[str]:
    events: list[str] = []
    tool._mg.home.side_effect = lambda: events.append("z_axis")
    tool._ms.home.side_effect = lambda: events.append("stage")
    tool._mx.home.side_effect = lambda: events.append("x_axis")
    tool._my.home.side_effect = lambda: events.append("y_axis")
    tool._home_rotate_sequence = AsyncMock(side_effect=lambda: events.append("rotate"))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize("isolate", [False, True])
async def test_home_uses_same_motor_order_for_startup_and_manual_homing(isolate: bool):
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._handle_fault = AsyncMock()
    events = _record_home_order(tool)

    await tool.home(isolate=isolate)

    # X homes again once the tool is straight: the first X home may have stopped on an upright.
    assert events == ["z_axis", "stage", "x_axis", "rotate", "x_axis", "y_axis"]


@pytest.mark.asyncio
@pytest.mark.parametrize("isolate", [False, True])
async def test_home_without_a_rotate_axis_homes_x_once(isolate: bool):
    tool = _make_tool()
    tool._handle_fault = AsyncMock()
    tool._emit_init = lambda *args: None
    events = _record_home_order(tool)

    await tool.home(isolate=isolate)

    assert events == ["z_axis", "stage", "x_axis", "y_axis"]


@pytest.mark.asyncio
async def test_isolated_home_keeps_the_provisional_x_home_when_rotate_fails():
    """With the tool angle still unknown a second X home could false-home on the same upright."""
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._emit_init = lambda *args: None
    events = _record_home_order(tool)
    tool._home_rotate_sequence = AsyncMock(side_effect=RuntimeError("rotate fault"))

    await tool.home(isolate=True)

    assert events == ["z_axis", "stage", "x_axis", "y_axis"]




@pytest.mark.asyncio
async def test_isolated_home_skips_rotate_when_x_home_fails():
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._emit_init = lambda *args: None
    events = _record_home_order(tool)
    tool._mx.home.side_effect = RuntimeError("x fault")

    await tool.home(isolate=True)

    assert "rotate" not in events
    assert events == ["z_axis", "stage", "y_axis"]
