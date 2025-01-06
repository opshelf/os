from collections.abc import Callable, Coroutine
from typing import Any
from perpetual_can import CANBusHandler
from .base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c.robot import P0C, RobotConfiguration
import asyncio
from typing import List, Optional
from perpetual_p0c.robot import RobotCommands, RobotResponses


class RobotClient(AbstractRobotClient):
    def __init__(
        self,
        command_queue: asyncio.Queue[RobotCommands],
        emergency_command: Callable[[RobotCommands], Coroutine[Any, Any, None]],
    ):
        self._command_queue = command_queue
        self._emergency_command = emergency_command

    async def send_command(self, command: RobotCommands) -> None:
        await self._command_queue.put(command)
        print(f"Command queued: {command}")

    async def send_emergency_command(self, command: RobotCommands) -> None:
        await self._emergency_command(command)

    async def get_queued_commands(self) -> List[RobotCommands]:
        return list(self._command_queue._queue)  # type: ignore

    async def handle_response(self, response: RobotResponses) -> None:
        print(f"Handling response: {response}")


class RobotWorker(AbstractRobotWorker):
    _instance: Optional["RobotWorker"] = None

    def __init__(self, robot: P0C):
        if RobotWorker._instance is not None:
            raise RuntimeError("RobotWorker is a singleton. Use `get_instance()` to access it.")

        # Shared queues
        self._command_queue: asyncio.Queue[RobotCommands] = asyncio.Queue()

        # Client shares the same queues
        self.client = RobotClient(self._command_queue, self.emergency_command)
        self._robot = robot
        self._current_command: Optional[RobotCommands] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self.running = False

    @classmethod
    async def get_instance(
        cls, bus: CANBusHandler, configuration: RobotConfiguration
    ) -> "RobotWorker":
        if cls._instance is None:
            robot = await P0C.build(bus, configuration)
            # await robot.initialize()
            cls._instance = cls(robot)
        return cls._instance

    async def get_client(self) -> AbstractRobotClient:
        return self.client

    async def process_commands(self) -> None:
        while self.running:
            try:
                if not self._command_queue.empty() and self._current_task is None:
                    command = await self._command_queue.get()
                    self._current_command = command
                    print(f"Processing command: {command}")
                    self._current_task = asyncio.create_task(self._robot.handle_command(command))
                    # await self._robot.handle_command(command)
                    self._current_command = None
                else:
                    if self._current_task and self._current_task.done():
                        self._current_task = None
                    await asyncio.sleep(0.05)

            except asyncio.CancelledError:
                print("Command processing task cancelled.")
                break

    async def start_worker(self) -> None:
        if not self.running:
            self._running = True
            self._worker_task = asyncio.create_task(self.process_commands())
            print("RobotWorker started.")

    async def emergency_command(self, command: RobotCommands) -> None:
        if self._current_task:
            self._current_task.cancel()
        response = await self._robot.handle_command(command)
        print(command, response)

    async def queue_command(self, command: RobotCommands) -> None:
        await self._command_queue.put(command)

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                await self._worker_task
            print("RobotWorker shut down.")
