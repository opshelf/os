"""The worker must never release the motion interlock while a safety input holds it.

lb4a-2 (2026-11-06): with the e-stop held through a reboot, RobotWorker.start() released the
interlock 150 ms after the e-stop monitor engaged it, and the following home and moves ran.
"""
import asyncio
import json
from contextlib import asynccontextmanager


from hardware.curtain import MotionSafetyInterlock
from openshelf_os.robot.commands import HomeCommand
from openshelf_os.robot.status import RobotCommandSuccessful
from openshelf_os.robot.worker import PHYSICAL_ESTOP_REASON, RobotWorker


class Pin:
    def __init__(self, value: bool) -> None:
        self.value = value

    def read(self) -> bool:
        return self.value


class InterlockRobot:
    def __init__(self, *, estop: bool = False) -> None:
        self._motion_safety = MotionSafetyInterlock()
        self.estop = Pin(estop)
        self._gpio = self
        self.curtain_blocked = False
        self.io_door_open = False
        self.cabinet_doors: dict[str, bool] | None = None
        self.handled: list[str] = []
        self.moved: list[str] = []

    def has_pin(self, name: str) -> bool:
        return name == "estop"

    def input(self, name: str) -> Pin:
        return self.estop

    def block_motion_for_safety(self) -> None:
        self._motion_safety.block()

    def release_motion_safety(self) -> None:
        self._motion_safety.unblock()

    def get_curtain_state(self) -> bool:
        return self.curtain_blocked

    async def refresh_door_state(self) -> bool:
        return self.io_door_open

    def get_cabinet_door_state(self) -> dict[str, bool] | None:
        return self.cabinet_doors

    def get_operation_context(self) -> None:
        return None

    async def read_tray_held(self) -> bool:
        return False

    async def handle_command(self, command) -> RobotCommandSuccessful:
        if command.command == "stop":
            return RobotCommandSuccessful(command=command.command)
        self.handled.append(command.command)
        # A motor move waits on the interlock before it starts the drive.
        await self._motion_safety.wait_clear()
        self.moved.append(command.command)
        return RobotCommandSuccessful(command=command.command)

    async def get_status(self) -> None:
        return None

    async def get_scale_data(self) -> None:
        return None

    def clear_operation_context(self) -> None:
        pass

    async def start_weight_guard(self) -> None:
        pass

    async def stop_weight_guard(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@asynccontextmanager
async def started(robot: InterlockRobot):
    RobotWorker._instance = None
    worker = RobotWorker(robot)
    events = await worker.subscribe_events()
    try:
        await worker.start()
        yield worker, events
    finally:
        await worker.shutdown()
        RobotWorker._instance = None


async def _home_result(worker: RobotWorker, events: asyncio.Queue[str]) -> dict:
    await worker.enqueue_command(HomeCommand())
    while True:
        event = json.loads(await asyncio.wait_for(events.get(), timeout=2))
        if event.get("command") == "home" and event.get("message") in ("success", "failed"):
            return event


async def test_start_keeps_the_interlock_blocked_while_the_estop_is_pressed() -> None:
    robot = InterlockRobot(estop=True)
    # configure() leaves it blocked after a startup safety trip.
    robot.block_motion_for_safety()

    async with started(robot) as (worker, _events):
        assert worker._is_emergency_stopped is True
        assert robot._motion_safety.blocked is True


async def test_start_keeps_the_interlock_blocked_on_a_crashed_boot_with_a_startup_hazard() -> None:
    # Stopped and idle, the worker ignores the curtain, so only the crash keeps start() from releasing.
    robot = InterlockRobot()
    robot.curtain_blocked = True
    robot.io_door_open = True
    robot.block_motion_for_safety()
    RobotWorker._instance = None
    worker = RobotWorker(robot)
    worker.set_crash_state("Motor 5 faulted")
    try:
        await worker.start()

        assert worker.get_safety_status()["active_sources"] == []
        assert robot._motion_safety.blocked is True
    finally:
        await worker.shutdown()
        RobotWorker._instance = None


async def test_start_keeps_the_interlock_blocked_while_a_cabinet_door_is_open() -> None:
    # Nothing re-blocks after start(): the guard only blocks on its own inactive-to-active change.
    robot = InterlockRobot()
    robot.cabinet_doors = {"open": True}
    robot.block_motion_for_safety()

    async with started(robot) as (worker, _events):
        await worker.enqueue_command(HomeCommand())
        await asyncio.sleep(0.2)

        assert worker.get_safety_status()["active_sources"] == ["cabinet_doors"]
        assert robot._motion_safety.blocked is True
        assert robot.moved == []


async def test_home_is_refused_while_the_estop_is_pressed_and_releasing_it_moves_nothing() -> None:
    robot = InterlockRobot(estop=True)

    async with started(robot) as (worker, events):
        result = await _home_result(worker, events)

        assert result["code"] == "emergency_stop"
        assert result["error"] == PHYSICAL_ESTOP_REASON
        robot.estop.value = False
        await asyncio.sleep(0.2)
        assert robot.handled == []
        assert robot.moved == []
        assert robot._motion_safety.blocked is True


async def test_recovery_home_while_crashed_is_held_by_a_blocked_light_curtain() -> None:
    robot = InterlockRobot()

    async with started(robot) as (worker, _events):
        worker.set_crash_state("Motor 5 faulted")
        robot.curtain_blocked = True
        robot.io_door_open = True

        await worker.enqueue_command(HomeCommand())
        for _ in range(40):
            if robot.handled:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        assert robot.handled == ["home"]
        assert robot.moved == []
        assert robot._motion_safety.blocked is True
        assert worker.get_safety_status()["active_sources"] == ["light_curtain"]


async def test_recovery_home_resumes_after_the_curtain_clears_and_the_operator_acknowledges() -> None:
    robot = InterlockRobot()

    async with started(robot) as (worker, _events):
        worker.set_crash_state("Motor 5 faulted")
        robot.curtain_blocked = True
        robot.io_door_open = True

        await worker.enqueue_command(HomeCommand())
        for _ in range(40):
            if robot.handled:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)
        assert robot._motion_safety.blocked is True

        robot.curtain_blocked = False
        robot.io_door_open = False
        assert (await worker.acknowledge_safety_guard())["accepted"] is True
        for _ in range(40):
            if robot.moved:
                break
            await asyncio.sleep(0.05)

        assert robot.moved == ["home"]
        assert robot._motion_safety.blocked is False
