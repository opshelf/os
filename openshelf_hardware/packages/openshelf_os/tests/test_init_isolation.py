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
    tool._mx._is_homed = False
    tool._mx._is_home_provisional = False
    tool._my = AsyncMock()
    tool._mg = AsyncMock()
    tool._ms = AsyncMock()
    tool._mr = None
    return tool


def _record_home_order(tool: Tool) -> list[str]:
    events: list[str] = []
    tool._mg.home.side_effect = lambda **_: events.append("z_axis")
    tool._ms.home.side_effect = lambda **_: events.append("stage")

    def home_x(provisional: bool = False) -> None:
        events.append("x_axis")
        tool._mx._is_homed = True
        tool._mx._is_home_provisional = provisional

    tool._mx.home.side_effect = home_x
    tool._my.home.side_effect = lambda **_: events.append("y_axis")
    tool._home_rotate_sequence = AsyncMock(side_effect=lambda: events.append("rotate"))
    return events


@pytest.mark.asyncio
async def test_tool_only_home_leaves_the_gantry_where_it_is():
    """A reset after a gripper feedback fault re-zeroes the tool without moving the gantry."""
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._emit_init = lambda *args: None
    events = _record_home_order(tool)

    await tool.home(axes=["z_axis", "stage", "rotate"])

    assert events == ["z_axis", "stage", "rotate"]


@pytest.mark.asyncio
async def test_home_all_rehomes_an_already_homed_x_provisionally_before_rotate():
    """An emergency stop can leave _is_homed True with stale coordinates; the clearance move needs a fresh X home."""
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._mx._is_homed = True
    tool._rotation_off_x_pose = AsyncMock(return_value=None)
    events = _record_home_order(tool)

    await tool.home()

    assert events == ["z_axis", "stage", "x_axis", "rotate", "y_axis"]
    tool._mx.home.assert_awaited_once_with(provisional=True)


@pytest.mark.asyncio
async def test_home_turns_an_off_pose_rotate_straight_before_rehoming_the_homed_x():
    """After an aborted calibration the X guard refuses a homed X while rotate is ~6 deg off its pose."""
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._mx._is_homed = True
    tool._rotation_off_x_pose = AsyncMock(return_value=6.0)
    events = _record_home_order(tool)
    tool.rotate_to_home_orientation = AsyncMock(side_effect=lambda: events.append("straighten"))

    await tool.home()

    assert events == ["z_axis", "stage", "straighten", "x_axis", "rotate", "y_axis"]


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
    tool._mx.home.assert_awaited_once_with(provisional=True)
    assert tool._mx._is_home_provisional is True


@pytest.mark.asyncio
async def test_isolated_home_keeps_the_provisional_x_home_when_rotate_dependency_fails():
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._emit_init = lambda *args: None
    events = _record_home_order(tool)
    tool._ms.home.side_effect = RuntimeError("stage fault")

    await tool.home(isolate=True)

    assert "rotate" not in events
    assert events == ["z_axis", "x_axis", "y_axis"]
    assert tool._mx.home.await_count == 1


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
    assert tool._mx.home.await_count == 1
