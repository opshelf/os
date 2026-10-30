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
from openshelf_os.robot.commands import HomeCommand, RecoverCommand, RetrieveItemCommand, RobotCommands
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
        if self.fault is not None and command.command not in ("home", "recover"):  # type: ignore[attr-defined]
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


async def test_recover_opens_the_gripper_before_the_tool_withdraws_and_homes_only_given_axes():
    """A failed grip can leave the fingers on the tray; withdrawing first would drag it."""
    robot = Robot.__new__(Robot)
    order: list[str] = []
    robot._operation_context = {}
    robot._tool = SimpleNamespace(
        _gs=SimpleNamespace(close=AsyncMock(side_effect=lambda: order.append("open fingers"))),
        retract_empty=AsyncMock(side_effect=lambda: order.append("retract")),
        home=AsyncMock(side_effect=lambda **kwargs: order.append(f"home {kwargs['axes']}")),
    )
    robot._io = SimpleNamespace(
        get_door_state=lambda: True,
        home_door=AsyncMock(side_effect=lambda: order.append("close door")),
        ensure_door_homed=AsyncMock(),
    )

    await robot.handle_command(RecoverCommand(axes=["y_axis"]))

    assert order == ["open fingers", "retract", "close door", "home ['y_axis']"]


@pytest.mark.parametrize("failure_code", [GRIP_PICK_FAILED_CODE, GRIP_IO_BOX_FAILED_CODE])
async def test_grip_fault_cancels_the_command_and_recovers_without_homing(failure_code):
    async with _crashed_by_grip_fault(failure_code) as (worker, events, failure):
        assert failure["message"] == "failed"
        assert failure["code"] == failure_code
        # No motor lost its position, so nothing is reported as needing a home, yet motion
        # still waits for the operator's reset.
        assert worker.needs_home() is False
        assert worker.needs_reset() is True
        assert worker._crash_code == failure_code

        await worker.enqueue_command(RecoverCommand())
        await _next_event(events, lambda e: e.get("command") == "recover")

        assert worker.needs_reset() is False
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
    """The grip code resets without a home, so a whole-machine stop must not keep it."""
    async with _crashed_by_grip_fault() as (worker, _events, _failure):
        assert worker._crash_code == GRIP_PICK_FAILED_CODE

        worker.set_needs_home(PHYSICAL_ESTOP_REASON)

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
    ("fault", "flagged", "expected_axes", "is_unknown", "is_crashed"),
    [
        # A drive fault whose effect is not classified leaves every axis untrusted, even when
        # another motor already holds a lost-position flag.
        (MovementError("Tool extension failed: Movement failed (status=FAULT)"), ["y_axis"], [], True, True),
        # A grip fault homes only a motor its cancelled sibling move left without a position.
        (
            GripVerificationError("Pick operation failed: No grip on grip", failure_code=GRIP_PICK_FAILED_CODE),
            ["door"],
            ["door"],
            False,
            True,
        ),
        # A servo error may be a dropped pwr_out0 rail that also reset the gantry drives.
        (GripperError("Pick operation failed: Modbus timeout"), [], [], True, True),
        # The rotation guard moves nothing; a sibling door home it cancelled still has to home.
        (_rotation_clearance_refusal(), ["door"], ["door"], False, False),
        (_rotation_clearance_refusal(), [], [], False, False),
    ],
)
async def test_a_classified_fault_homes_only_the_axes_it_invalidated(
    fault, flagged, expected_axes, is_unknown, is_crashed
):
    robot = PickRobot(fault)
    robot._tool = SimpleNamespace(  # type: ignore[attr-defined]
        _mx=_motor(1), _my=_motor(2), _mg=_motor(3), _ms=_motor(4), _mr=_motor(5)
    )
    robot._io = SimpleNamespace(_md=_motor(6))  # type: ignore[attr-defined]
    motors = {"y_axis": robot._tool._my, "stage": robot._tool._ms, "door": robot._io._md}
    for axis in flagged:
        motors[axis]._is_homed = False

    RobotWorker._instance = None
    worker = RobotWorker(robot)  # type: ignore[arg-type]
    events = await worker.subscribe_events()
    await worker.enqueue_command(RetrieveItemCommand(location=LOCATION), command_id=COMMAND_ID)
    worker._running = True
    task = asyncio.create_task(worker.process_commands())
    try:
        await _next_event(events, lambda e: e.get("command") == "retrieve_item")

        assert worker.needs_home_axes() == expected_axes
        assert worker._needs_home_unknown is is_unknown
        assert worker.needs_home() is bool(expected_axes or is_unknown)
        assert worker._is_crashed is is_crashed
        # The recover reset homes the reported axes, so the grip code survives them.
        if isinstance(fault, GripVerificationError):
            assert worker._crash_code == GRIP_PICK_FAILED_CODE
    finally:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        RobotWorker._instance = None
