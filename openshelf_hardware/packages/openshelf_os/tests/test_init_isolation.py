from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from openshelf_os.iobox import IOBox, IOBoxStatus
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
    tool.restore_y_brake_output = AsyncMock()
    return tool


def _record_home_order(tool: Tool) -> list[str]:
    events: list[str] = []
    tool._mg.home.side_effect = lambda: events.append("z_axis")
    tool._ms.home.side_effect = lambda: events.append("stage")
    tool._mx.home.side_effect = lambda: events.append("x_axis")
    tool._my.home.side_effect = lambda: events.append("y_axis")
    tool._home_rotate_sequence = AsyncMock(side_effect=lambda: events.append("rotate"))
    return events


def _make_iobox() -> IOBox:
    iobox = IOBox.__new__(IOBox)
    iobox._logger = logging.getLogger("test_init_isolation")
    iobox._init_reporter = None
    iobox._md = None
    iobox._scale_enabled = True
    iobox._scale_ready = False
    iobox._scale_bus_ready = True
    iobox._scale = AsyncMock()
    iobox._status = IOBoxStatus.IDLE
    iobox.home_door = AsyncMock()
    iobox.refresh_door_state = AsyncMock()
    iobox._configure_curtain = AsyncMock()
    return iobox


@pytest.mark.asyncio
async def test_home_isolate_continues_past_failing_axis():
    tool = _make_tool()
    tool._mg.home.side_effect = RuntimeError("z_axis faulted")

    await tool.home(isolate=True)

    tool._ms.home.assert_awaited_once()
    tool._mg.home.assert_awaited_once()
    tool._mx.home.assert_awaited_once()
    tool._my.home.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("isolate", [False, True])
async def test_home_uses_same_motor_order_for_startup_and_manual_homing(isolate: bool):
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._handle_fault = AsyncMock()
    events = _record_home_order(tool)

    await tool.home(isolate=isolate)

    assert events == ["z_axis", "stage", "rotate", "x_axis", "y_axis"]


@pytest.mark.asyncio
async def test_home_non_isolate_raises_on_failure():
    tool = _make_tool()
    tool._handle_fault = AsyncMock()
    tool._mg.home.side_effect = RuntimeError("z_axis faulted")

    with pytest.raises(Exception):
        await tool.home(isolate=False)

    tool._mx.home.assert_not_awaited()
    tool._handle_fault.assert_awaited_once()


@pytest.mark.asyncio
async def test_configure_axis_isolated_swallows_failure():
    tool = _make_tool()
    motor = AsyncMock()
    motor.configure.side_effect = RuntimeError("config failed")

    did_configure = await tool._configure_axis_isolated(motor, isolate=True)

    assert did_configure is False
    motor.configure.assert_awaited_once()

    motor.configure.reset_mock()
    motor.configure.side_effect = RuntimeError("config failed")
    with pytest.raises(Exception):
        await tool._configure_axis_isolated(motor, isolate=False)


@pytest.mark.asyncio
async def test_home_isolate_skips_failed_config_motor():
    tool = _make_tool()

    await tool.home(isolate=True, failed_config_motors=[tool._mg])

    tool._ms.home.assert_awaited_once()
    tool._mg.home.assert_not_awaited()
    tool._mx.home.assert_awaited_once()
    tool._my.home.assert_awaited_once()


@pytest.mark.asyncio
async def test_home_isolate_reports_rotate_failed_when_dependency_failed_config():
    tool = _make_tool()
    tool._mr = AsyncMock()
    events = []
    tool._init_reporter = lambda component, phase, detail="", reason=None: events.append(
        (component, phase, detail, reason)
    )

    await tool._home_rotate_isolated(failed_config_motors=[tool._mx])

    assert ("rotate", "failed", "home", "dependency failed") in events
    tool._mr.home.assert_not_awaited()


@pytest.mark.asyncio
async def test_home_isolate_reports_rotate_failed_when_dependency_home_failed():
    tool = _make_tool()
    tool._mr = AsyncMock()
    tool._ms.home.side_effect = RuntimeError("stage faulted")
    events = []
    tool._init_reporter = lambda component, phase, detail="", reason=None: events.append(
        (component, phase, detail, reason)
    )

    await tool.home(isolate=True)

    assert ("rotate", "failed", "home", "dependency failed") in events
    tool._mr.home.assert_not_awaited()


@pytest.mark.asyncio
async def test_home_isolate_does_not_report_rotate_dependency_when_rotate_failed_config():
    tool = _make_tool()
    tool._mr = AsyncMock()
    events = []
    tool._init_reporter = lambda component, phase, detail="", reason=None: events.append(
        (component, phase, detail, reason)
    )

    await tool._home_rotate_isolated(failed_config_motors=[tool._mr, tool._mx])

    assert events == []
    tool._mr.home.assert_not_awaited()


@pytest.mark.asyncio
async def test_iobox_configure_homes_door_once_via_home_door():
    iobox = _make_iobox()
    iobox._md = AsyncMock()

    await iobox.configure(isolate=True, home_on_init=True)

    iobox._md.configure.assert_awaited_once()
    iobox._md.home.assert_not_awaited()
    iobox.home_door.assert_awaited_once()


@pytest.mark.asyncio
async def test_iobox_configure_keeps_scale_down_when_bus_unavailable():
    iobox = _make_iobox()
    events = []
    iobox._init_reporter = lambda component, phase, detail="", reason=None: events.append(
        (component, phase, reason)
    )

    await iobox.configure(isolate=True, scale_bus_ready=False)

    assert iobox._scale_ready is False
    assert ("scale", "failed", "scale bus unavailable") in events


@pytest.mark.asyncio
async def test_iobox_scale_read_does_not_poll_when_bus_unavailable():
    iobox = _make_iobox()
    iobox._scale_bus_ready = False

    weight = await iobox.get_weight()

    assert weight is None
    iobox._scale.wait_for_stable_weight.assert_not_awaited()
