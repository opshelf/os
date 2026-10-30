"""A failed grip check cancels the command and homes only the axes it invalidated."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import TypeAdapter, ValidationError

from openshelf_os.exceptions import (
    GRIP_IO_BOX_FAILED_CODE,
    GRIP_PICK_FAILED_CODE,
    GripperError,
    GripVerificationError,
    MovementError,
    RotationClearanceError,
)
from openshelf_os.robot.commands import HomeCommand, RetrieveItemCommand, RobotCommands
from openshelf_os.robot.core import Robot
from openshelf_os.robot.status import RobotCommandSuccessful
from openshelf_os.robot.worker import PHYSICAL_ESTOP_REASON, RobotWorker

LOCATION = {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}
COMMAND_ID = "00000000-0000-4000-8000-000000000001"


class PickRobot:
    def __init__(self, fault: Exception | None) -> None:
        self.fault = fault

    def block_motion_for_safety(self) -> None:
        pass

    def release_motion_safety(self) -> None:
        pass

    async def get_status(self) -> None:
        return None

    async def handle_command(self, command: object) -> RobotCommandSuccessful:
        if self.fault is not None and command.command != "home":  # type: ignore[attr-defined]
            raise self.fault
        return RobotCommandSuccessful(command=command.command)  # type: ignore[attr-defined]


async def _next_event(events, predicate):
    while True:
        event = json.loads(await asyncio.wait_for(events.get(), timeout=2))
        if predicate(event):
            return event


@asynccontextmanager
async def _crashed_by_grip_fault(failure_code=GRIP_PICK_FAILED_CODE):
    """A worker whose last command failed a grip check, its event queue and that failure event."""
    fault = GripVerificationError(
        "Pick operation failed: No grip on grip", failure_code=failure_code
    )
    RobotWorker._instance = None
    worker = RobotWorker(PickRobot(fault))  # type: ignore[arg-type]
    events = await worker.subscribe_events()
    await worker.enqueue_command(RetrieveItemCommand(location=LOCATION), command_id=COMMAND_ID)
    worker._running = True
    task = asyncio.create_task(worker.process_commands())
    try:
        failure = await _next_event(events, lambda e: e.get("command") == "retrieve_item")
        yield worker, events, failure
    finally:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        RobotWorker._instance = None


@pytest.mark.parametrize(
    ("extra_axis", "homes_door"),
    [("rotate", False), ("door", True)],
)
async def test_partial_home_homes_the_io_door_after_the_tool(extra_axis, homes_door):
    """The tool leaves the IO box before the door homes over it."""
    robot = Robot.__new__(Robot)
    order: list[str] = []
    robot._operation_context = {}
    robot._tool = SimpleNamespace(home=AsyncMock(side_effect=lambda **_: order.append("tool")))
    robot._io = SimpleNamespace(home_door=AsyncMock(side_effect=lambda: order.append("door")))

    await robot.handle_command(HomeCommand(axes=["z_axis", "stage", extra_axis]))

    assert order == (["tool", "door"] if homes_door is True else ["tool"])


@pytest.mark.parametrize(
    ("failure_code", "expected_axes"),
    [
        (GRIP_PICK_FAILED_CODE, ["rotate", "stage", "z_axis"]),
        # A multi-item tray fault leaves the tool in the IO box, so the door homes too.
        (GRIP_IO_BOX_FAILED_CODE, ["door", "rotate", "stage", "z_axis"]),
    ],
)
async def test_grip_fault_cancels_the_command_and_homes_only_what_it_invalidated(
    failure_code, expected_axes
):
    async with _crashed_by_grip_fault(failure_code) as (worker, events, failure):
        assert failure["message"] == "failed"
        assert failure["code"] == failure_code
        # The gantry kept its position, so a reset must not home the whole machine.
        assert worker.needs_home_axes() == expected_axes
        assert worker._crash_code == failure_code

        await worker.enqueue_command(HomeCommand(axes=expected_axes))
        await _next_event(events, lambda e: e.get("command") == "home")

        assert worker.needs_home() is False
        assert worker._is_crashed is False


async def test_partial_home_keeps_a_needs_home_it_cannot_attribute_to_axes():
    """After an e-stop every axis is untrusted, so a tool-only home must not open the interlock."""
    # An empty list would home nothing yet still count as a successful home.
    with pytest.raises(ValidationError):
        TypeAdapter(RobotCommands).validate_python({"command": "home", "axes": []})

    RobotWorker._instance = None
    worker = RobotWorker(PickRobot(None))  # type: ignore[arg-type]
    events = await worker.subscribe_events()
    worker.set_needs_home(PHYSICAL_ESTOP_REASON)
    await worker.enqueue_command(HomeCommand(axes=["z_axis", "stage", "rotate"]))
    worker._running = True
    task = asyncio.create_task(worker.process_commands())
    try:
        home = await _next_event(events, lambda e: e.get("command") == "home")

        assert home["message"] == "success"
        assert worker.needs_home() is True
    finally:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        RobotWorker._instance = None


async def test_an_estop_after_a_grip_fault_widens_the_reset_and_drops_the_grip_code():
    """The grip code narrows a reset to the tool, so a whole-machine stop must not keep it."""
    async with _crashed_by_grip_fault() as (worker, _events, _failure):
        assert worker.needs_home_axes() == ["rotate", "stage", "z_axis"]
        assert worker._crash_code == GRIP_PICK_FAILED_CODE

        worker.set_needs_home(PHYSICAL_ESTOP_REASON)

        # Every axis is untrusted now, so the known axes stay reported and keep the debug motor
        # home from clearing the requirement after re-homing one of them.
        assert worker.needs_home_axes() == ["rotate", "stage", "z_axis"]
        assert worker.needs_home() is True
        assert worker.needs_home_reason() == PHYSICAL_ESTOP_REASON
        # The kiosk falls back to the full checklist and a full home.
        assert worker._crash_code is None


async def test_a_grip_fault_does_not_answer_for_a_later_power_cycle():
    """Cycling the tool rail while crashed loses the gantry, which the grip code cannot describe."""
    async with _crashed_by_grip_fault() as (worker, _events, _failure):
        # A power cycle of the rail feeding every drive, as POST /debug/power/pwr_out0/cycle does.
        worker.set_needs_home(
            "power cycle: pwr_out0", ["x_axis", "y_axis", "z_axis", "stage", "rotate"]
        )

        assert worker.needs_home_axes() == ["rotate", "stage", "x_axis", "y_axis", "z_axis"]
        assert worker._crash_code is None


def test_an_attributable_fault_adds_axes_without_taking_over_the_reason():
    """A debug home of one motor clears an unattributed needs-home whose reason names that motor."""
    RobotWorker._instance = None
    worker = RobotWorker(PickRobot(None))  # type: ignore[arg-type]
    worker.set_needs_home(PHYSICAL_ESTOP_REASON)

    # The calibration tool-motor home reports its failure with explicit axes.
    worker.set_needs_home(
        "Failed to home tool motors: Motor 4 faulted during homing", axes=["stage", "z_axis"]
    )

    assert worker.needs_home_axes() == ["stage", "z_axis"]
    assert worker.needs_home() is True
    assert worker.needs_home_reason() == PHYSICAL_ESTOP_REASON


def _motor(motor_id: int) -> SimpleNamespace:
    return SimpleNamespace(_motor_id=motor_id, _is_homed=True)


def _rotation_clearance_refusal() -> MovementError:
    refusal = RotationClearanceError("Unsafe tool clearance for rotation: z_axis=9mm")
    err = MovementError(f"Tool rotation failed: {refusal}")
    err.__cause__ = refusal
    return err


@pytest.mark.parametrize(
    ("fault", "stage_lost_position", "expected_axes", "is_crashed"),
    [
        # A wrapped drive fault never names its motor; the motor flagged its own lost position.
        (MovementError("Tool extension failed: Movement failed (status=FAULT)"), True, ["stage"], True),
        # A gripper servo fault stops the tool, not the gantry.
        (GripperError("Pick operation failed: Gripper fault: OVERLOAD"), False, ["rotate", "stage", "z_axis"], True),
        # The rotation guard refuses before anything moves.
        (_rotation_clearance_refusal(), False, [], False),
    ],
)
async def test_a_fault_homes_only_the_axes_that_lost_position(
    fault, stage_lost_position, expected_axes, is_crashed
):
    robot = PickRobot(fault)
    robot._tool = SimpleNamespace(  # type: ignore[attr-defined]
        _mx=_motor(1), _my=_motor(2), _mg=_motor(3), _ms=_motor(4), _mr=_motor(5)
    )
    robot._io = SimpleNamespace(_md=_motor(6))  # type: ignore[attr-defined]
    # The drive fault cleared the stage motor's homed flag, as Motor.wait_for_move does.
    robot._tool._ms._is_homed = stage_lost_position is False  # type: ignore[attr-defined]

    RobotWorker._instance = None
    worker = RobotWorker(robot)  # type: ignore[arg-type]
    events = await worker.subscribe_events()
    await worker.enqueue_command(RetrieveItemCommand(location=LOCATION), command_id=COMMAND_ID)
    worker._running = True
    task = asyncio.create_task(worker.process_commands())
    try:
        await _next_event(events, lambda e: e.get("command") == "retrieve_item")

        assert worker.needs_home_axes() == expected_axes
        assert worker.needs_home() is bool(expected_axes)
        assert worker._is_crashed is is_crashed
    finally:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        RobotWorker._instance = None
