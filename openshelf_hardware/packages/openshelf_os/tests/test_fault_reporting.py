import asyncio
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hardware.curtain import MotionSafetyInterlock
from hardware.motor import MotorStatus
from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import MovementError
from openshelf_os.tool import Tool, run_together


def build_fault_reporting_tool() -> Tool:
    tool = Tool.__new__(Tool)
    tool._logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    tool._mx = SimpleNamespace(
        wait_for_move=AsyncMock(return_value=MotorStatus.FAULT),
        _motor_id=1,
        _cur_pos=10.0,
        _target_pos=20.0,
        _status=MotorStatus.FAULT,
        _simulated_fault=True,
        is_homed=lambda: False,
        is_hard_stopped=lambda: True,
    )
    tool._my = SimpleNamespace(
        wait_for_move=AsyncMock(return_value=MotorStatus.STOPPED),
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
    x_cleanup.assert_awaited()
    y_cleanup.assert_awaited()


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
async def test_move_cancelled_during_its_own_error_cleanup_still_stops_both_axes() -> None:
    # The failure happens before gantry_wait, so only Tool.move's cleanup is running when
    # the cancellation lands; it must finish, and the stop failure must reach the caller.
    tool = build_fault_reporting_tool()
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    y_cleanup_finished = asyncio.Event()

    async def cleanup_x() -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        raise RuntimeError("x stop write failed")

    async def cleanup_y() -> None:
        await release_cleanup.wait()
        y_cleanup_finished.set()

    tool._ensure_retracted = AsyncMock(side_effect=RuntimeError("retract failed"))
    tool._mx._stop_and_clear_motion = cleanup_x
    tool._my._stop_and_clear_motion = cleanup_y

    move_task = asyncio.create_task(tool.move(coordinate))
    await cleanup_started.wait()
    move_task.cancel()
    await asyncio.sleep(0)
    assert not move_task.done()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await move_task

    assert y_cleanup_finished.is_set()
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert any("x stop write failed" in e for e in exc_info.value.fault_context["gantry_stop_errors"])


@pytest.mark.asyncio
async def test_failed_y_start_never_leaves_x_moving() -> None:
    # Both gantry axes start together; X must end stopped whether or not its trigger
    # landed before Y's start failed.
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

    # Let X's start deadline pass: a start left running would land after the stop.
    await asyncio.sleep(0.03)
    assert events and events[-1] == "x_stopped"


@pytest.mark.asyncio
async def test_hold_active_at_move_entry_does_not_spend_the_gantry_deadline() -> None:
    # Motor starts block on the interlock; a hold that predates the move must be waited out
    # before the gantry deadline starts, not reported as a gantry timeout (crash state).
    tool = build_fault_reporting_tool()
    tool._mx.wait_for_move.return_value = MotorStatus.STOPPED
    coordinate = AxisCoordinate(x=12.0, y=34.0, s=0, z_offset=0.0)
    safety = MotionSafetyInterlock()
    safety.block()
    started: list[str] = []

    def start_after_clear(axis: str):
        async def start(_: float) -> bool:
            await safety.wait_clear()
            started.append(axis)
            return True

        return start

    for axis, motor in (("x", tool._mx), ("y", tool._my)):
        motor._safety = safety
        motor.move_to_position_no_wait = start_after_clear(axis)
    tool.gantry_wait = partial(Tool.gantry_wait, tool, timeout_seconds=0.2)
    tool._handle_fault = AsyncMock()

    move_task = asyncio.create_task(tool.move(coordinate))
    await asyncio.sleep(0.4)
    assert not move_task.done()
    safety.unblock()
    await move_task

    assert sorted(started) == ["x", "y"]
    tool._handle_fault.assert_not_awaited()


def _stopping_sibling(fault_context: dict | None):
    async def sibling() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as cancelled:
            if fault_context is not None:
                cancelled.fault_context = fault_context
            raise

    return sibling()


async def _failing_step() -> None:
    await asyncio.sleep(0)
    raise MovementError("door failed")


@pytest.mark.asyncio
async def test_run_together_reports_cancelled_sibling_stop_failure(caplog) -> None:
    # A gantry move cancelled by its sibling's failure carries a failed stop write only as
    # fault_context on the CancelledError; it must not vanish behind the door error.
    stop_errors = {"gantry_stop_errors": ["x_axis: stop write failed"]}
    with caplog.at_level("WARNING", logger="openshelf_os.tool"):
        with pytest.raises(MovementError) as raised:
            await run_together(_stopping_sibling(stop_errors), _failing_step())

    assert any("stop write failed" in note for note in raised.value.__notes__)
    assert any("also failed while being stopped" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_together_ignores_plain_cancelled_sibling(caplog) -> None:
    with caplog.at_level("WARNING", logger="openshelf_os.tool"):
        with pytest.raises(MovementError) as raised:
            await run_together(_stopping_sibling(None), _failing_step())

    assert not getattr(raised.value, "__notes__", [])
    assert not caplog.records
