import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hardware.motor import MotorStatus
from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import MovementError
from openshelf_os.tool import Tool


def build_fault_reporting_tool() -> Tool:
    tool = Tool.__new__(Tool)
    tool._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    tool._mx = SimpleNamespace(
        wait_for_move=lambda **kwargs: MotorStatus.FAULT,
        _motor_id=1,
        _cur_pos=10.0,
        _target_pos=20.0,
        _status=MotorStatus.FAULT,
        _simulated_fault=True,
        is_homed=lambda: False,
        is_hard_stopped=lambda: True,
    )
    tool._my = SimpleNamespace(
        wait_for_move=lambda **kwargs: MotorStatus.STOPPED,
        _motor_id=2,
        _cur_pos=30.0,
        _target_pos=30.0,
        _status=MotorStatus.STOPPED,
        _simulated_fault=False,
        is_homed=lambda: True,
        is_hard_stopped=lambda: False,
    )
    tool._mg = None
    tool._ms = None
    tool._mr = None
    return tool


@pytest.mark.asyncio
async def test_move_cancellation_during_x_start_stops_and_clears_both_gantry_axes() -> None:
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    x_cleanup = AsyncMock()
    y_cleanup = AsyncMock()

    async def move_x(_: float) -> None:
        raise asyncio.CancelledError()

    tool._my.move_to_position_no_wait = AsyncMock()
    tool._mx.move_to_position_no_wait = move_x
    tool._mx._stop_and_clear_motion = x_cleanup
    tool._my._stop_and_clear_motion = y_cleanup

    with pytest.raises(asyncio.CancelledError):
        await tool.move(coordinate)

    tool._my.move_to_position_no_wait.assert_awaited_once_with(coordinate.y)
    x_cleanup.assert_awaited_once_with()
    y_cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_move_cancellation_during_error_cleanup_waits_for_both_gantry_axes() -> None:
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    x_cleanup_started = asyncio.Event()
    y_cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    x_cleanup_finished = asyncio.Event()
    y_cleanup_finished = asyncio.Event()

    async def move_x(_: float) -> None:
        raise RuntimeError("x start failed")

    async def cleanup_x() -> None:
        x_cleanup_started.set()
        await release_cleanup.wait()
        x_cleanup_finished.set()

    async def cleanup_y() -> None:
        y_cleanup_started.set()
        await release_cleanup.wait()
        y_cleanup_finished.set()

    tool._my.move_to_position_no_wait = AsyncMock()
    tool._mx.move_to_position_no_wait = move_x
    tool._mx._stop_and_clear_motion = cleanup_x
    tool._my._stop_and_clear_motion = cleanup_y

    move_task = asyncio.create_task(tool.move(coordinate))
    await x_cleanup_started.wait()
    await y_cleanup_started.wait()
    move_task.cancel()
    await asyncio.sleep(0)
    assert not move_task.done()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await move_task

    assert x_cleanup_finished.is_set()
    assert y_cleanup_finished.is_set()
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert not isinstance(exc_info.value, MovementError)


@pytest.mark.asyncio
async def test_failed_y_start_stops_x_only_after_its_concurrent_start_lands() -> None:
    # Both gantry axes start together; a cleanup that ran before X's trigger landed
    # would leave X moving unsupervised.
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    events: list[str] = []

    async def move_y(_: float) -> None:
        raise RuntimeError("y brake release failed")

    async def move_x(_: float) -> bool:
        await asyncio.sleep(0.01)
        events.append("x_triggered")
        return True

    async def cleanup_x() -> None:
        events.append("x_stopped")

    tool._my.move_to_position_no_wait = move_y
    tool._mx.move_to_position_no_wait = move_x
    tool._mx._stop_and_clear_motion = cleanup_x
    tool._my._stop_and_clear_motion = AsyncMock()
    tool._handle_fault = AsyncMock()

    with pytest.raises(MovementError):
        await tool.move(coordinate)

    assert events == ["x_triggered", "x_stopped"]
