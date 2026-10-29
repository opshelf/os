"""A tray left in the gripper blocks robot commands until a fresh gripper read shows the tool empty."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hardware.servo import Servo
from openshelf_os.exceptions import MotorError
from openshelf_os.robot.commands import RetrieveItemCommand
from openshelf_os.robot.core import STARTUP_HOME_SKIPPED_TRAY_ON_TOOL, Robot
from openshelf_os.robot.status import RobotCommandSuccessful
from openshelf_os.robot.worker import TRAY_ON_TOOL_CODE, RobotWorker
from openshelf_os.tool import Tool

LOCATION = {"side": "front_module", "shelf_idx": 0, "cell_idx": 0}
# Gripper status byte: activation bit 5, gOBJ in bits 6-7.
HELD = 0x20 | (1 << 6)
EMPTY = 0x20 | (3 << 6)
UNPOWERED = 0x00


class GripperBus:
    def __init__(self, status: int) -> None:
        self.status = status
        self.writes: list[tuple] = []

    async def read_holding_register_block(self, _motor_id: int, _register: int, _count: int) -> list[int]:
        return [self.status, 0, 0]

    async def write_single_register(self, *args: int) -> None:
        self.writes.append(args)


def make_tool(bus: GripperBus) -> Tool:
    tool = Tool.__new__(Tool)
    tool._params = SimpleNamespace(gripper=SimpleNamespace(enabled=True))
    tool._gs = Servo(bus)  # type: ignore[arg-type]
    tool.is_grip_detection_enabled = True
    tool._logger = logging.getLogger("test_tray_on_tool")
    tool._init_reporter = None
    tool._mx = tool._my = tool._mg = tool._ms = tool._mr = None
    tool._laser = None
    tool.home = AsyncMock()  # type: ignore[method-assign]
    return tool


class ToolRobot:
    """Runs commands against a real Tool's gripper; the first faults with the tray gripped."""

    def __init__(self, tool: Tool, bus: GripperBus) -> None:
        self._tool = tool
        self._bus = bus
        self.handled: list[str] = []

    def block_motion_for_safety(self) -> None:
        pass

    def release_motion_safety(self) -> None:
        pass

    async def get_status(self) -> None:
        return None

    async def read_tray_held(self) -> bool:
        return await self._tool.read_tray_held()

    async def handle_command(self, command) -> RobotCommandSuccessful:
        self.handled.append(command.command)
        if len(self.handled) == 1:
            self._bus.status = HELD
            raise MotorError("IO door fault during retrieve")
        return RobotCommandSuccessful(command=command.command)


async def _retrieve(worker: RobotWorker, events: asyncio.Queue[str]) -> dict:
    await worker.enqueue_command(RetrieveItemCommand(location=LOCATION))
    while True:
        event = json.loads(await asyncio.wait_for(events.get(), timeout=2))
        if event.get("command") == "retrieve_item":
            return event


@pytest.fixture
async def running_worker():
    workers: list[tuple[RobotWorker, asyncio.Task]] = []

    async def start(robot) -> tuple[RobotWorker, asyncio.Queue[str]]:
        RobotWorker._instance = None
        worker = RobotWorker(robot)
        events = await worker.subscribe_events()
        worker._running = True
        workers.append((worker, asyncio.create_task(worker.process_commands())))
        return worker, events

    yield start
    for worker, task in workers:
        worker._running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    RobotWorker._instance = None


async def test_held_tray_after_a_fault_blocks_commands_until_the_gripper_reads_empty(running_worker):
    bus = GripperBus(EMPTY)
    robot = ToolRobot(make_tool(bus), bus)
    worker, events = await running_worker(robot)

    fault = await _retrieve(worker, events)
    assert fault["message"] == "failed"
    assert worker.tray_on_tool() is True

    blocked = await _retrieve(worker, events)
    assert blocked["code"] == TRAY_ON_TOOL_CODE
    assert robot.handled == ["retrieve_item"]

    # A gripper that lost power reports no object until activated: that is no evidence of an
    # empty tool, so the last known hold stands.
    bus.status = UNPOWERED
    assert (await _retrieve(worker, events))["code"] == TRAY_ON_TOOL_CODE

    bus.status = EMPTY
    assert (await _retrieve(worker, events))["message"] == "success"
    assert worker.tray_on_tool() is False
    assert robot.handled == ["retrieve_item", "retrieve_item"]


async def test_startup_with_a_held_tray_skips_homing_and_blocks_commands(running_worker):
    bus = GripperBus(HELD)
    tool = make_tool(bus)
    robot = Robot.__new__(Robot)
    robot._logger = logging.getLogger("test_tray_on_tool")
    robot._emit_init = lambda *_args, **_kwargs: None
    robot._connect_controller = AsyncMock()
    robot._bus_settings = {"motor": None}
    robot._motorbus = robot._scalebus = object()
    robot._config = SimpleNamespace(get=lambda *_args: SimpleNamespace(enabled=False))
    robot._gpio = None
    robot._weight_guard = None
    robot._cabinet = None
    robot._tool = tool
    robot._io = SimpleNamespace(configure=AsyncMock())
    robot._get_motor_by_name = lambda _name: None

    reason = await robot.configure(isolate=True, home_on_init=True)

    assert reason == STARTUP_HOME_SKIPPED_TRAY_ON_TOOL
    tool.home.assert_not_awaited()
    assert robot._io.configure.await_args.kwargs["home_on_init"] is False
    # Re-activating the gripper could move the fingers and drop the tray.
    assert bus.writes == []

    robot.handle_command = AsyncMock()  # type: ignore[method-assign]
    worker, events = await running_worker(robot)

    assert (await _retrieve(worker, events))["code"] == TRAY_ON_TOOL_CODE
    robot.handle_command.assert_not_awaited()
