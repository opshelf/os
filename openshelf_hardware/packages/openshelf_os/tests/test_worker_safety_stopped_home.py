"""A home a safety guard stopped leaves a home owed, not a crash (lb4a-3 test 3)."""
from __future__ import annotations

import asyncio
import json

import pytest

from hardware.exceptions import MotorError as HardwareMotorError
from hardware.exceptions import MotorSafetyStopError
from openshelf_os.exceptions import MotorError
from openshelf_os.faults import FAULT_MESSAGES
from openshelf_os.robot.commands import DeliverTrayCommand, HomeCommand
from openshelf_os.robot.status import RobotCommandSuccessful
from openshelf_os.robot.worker import RobotWorker

COMMAND_ID = "00000000-0000-4000-8000-000000000003"


def tool_home_error(cause: Exception) -> MotorError:
    """The error as the tool raises it: its own MotorError wrapping the motor's."""
    try:
        try:
            raise cause
        except Exception as inner:
            raise MotorError(f"Failed to home tool: {inner}") from inner
    except MotorError as wrapped:
        return wrapped


class HomeRobot:
    def __init__(self, fault: Exception) -> None:
        self.fault = fault

    def block_motion_for_safety(self) -> None:
        pass

    def release_motion_safety(self) -> None:
        pass

    async def get_status(self) -> None:
        return None

    async def handle_command(self, command: object) -> RobotCommandSuccessful:
        if command.command != "stop":  # type: ignore[attr-defined]
            raise self.fault
        return RobotCommandSuccessful(command=command.command)  # type: ignore[attr-defined]


async def run_failed_command(fault: Exception, command: object | None = None):
    RobotWorker._instance = None
    worker = RobotWorker(HomeRobot(fault))  # type: ignore[arg-type]
    reports: list[object] = []
    worker._report_error = lambda error, **_: reports.append(error)  # type: ignore[method-assign]
    worker._queue_motion_crash_report = lambda error, **_: reports.append(error)  # type: ignore[method-assign]
    events = await worker.subscribe_events()
    command = command or HomeCommand()
    await worker.enqueue_command(command, command_id=COMMAND_ID)
    worker._running = True
    task = asyncio.create_task(worker.process_commands())
    try:
        while True:
            event = json.loads(await asyncio.wait_for(events.get(), timeout=2))
            if event.get("command") == command.command:  # type: ignore[attr-defined]
                return worker, event, reports
    finally:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        RobotWorker._instance = None


async def test_a_home_stopped_by_a_safety_guard_owes_a_home_without_crashing():
    # The door opened during the reset home. As a hardware fault it crashed the robot and
    # sent a crash report, so the kiosk showed the crash dialog for a door interruption.
    stop = MotorSafetyStopError("Homing of motor 5 was stopped by the safety guard and did not complete")
    stop.fault_context = {"alarm_code": 0}  # type: ignore[attr-defined]  # as Tool._handle_fault stamps it
    fault = tool_home_error(stop)

    worker, failure, reports = await run_failed_command(fault)

    assert worker._is_crashed is False
    assert worker.needs_home() is True
    assert reports == []
    assert failure["fault_code"] == "safety_stopped"
    assert failure["error"] == FAULT_MESSAGES["safety_stopped"]


async def test_a_home_that_faults_on_its_own_still_crashes():
    fault = tool_home_error(HardwareMotorError("Homing of motor 5 failed: drive alarm"))

    worker, failure, reports = await run_failed_command(fault)

    assert worker._is_crashed is True
    assert len(reports) == 1
    assert failure["fault_code"] != "safety_stopped"


async def test_a_tray_workflow_home_stopped_by_a_safety_guard_still_crashes():
    # The persisted workflow fails on `execution_error` and strands the tray outside, so the
    # worker must crash with it or the next restart crashes it from the stale workflow.
    fault = MotorSafetyStopError("Homing of motor 5 was stopped by the safety guard and did not complete")
    command = DeliverTrayCommand(barcode="123", location={"side": "front_module", "shelf_idx": 0, "cell_idx": 2})

    worker, _, reports = await run_failed_command(tool_home_error(fault), command)

    assert worker._is_crashed is True
    assert len(reports) == 1

