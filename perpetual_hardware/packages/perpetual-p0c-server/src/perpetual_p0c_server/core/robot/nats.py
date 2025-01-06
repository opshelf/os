from collections.abc import Callable, Coroutine
from typing import Any
from perpetual_can import CANBusHandler
from .base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c.robot import P0C, RobotConfiguration
import asyncio
from typing import List, Optional
from perpetual_p0c.robot import RobotCommands, RobotResponses
from nats import NATS


"""
class RobotWorker:
    def __init__(self, nats_url: str, work_queue: str, response_queue: str, emergency_queue: str) -> None:
        self.nats_url = nats_url
        self.work_queue = work_queue
        self.response_queue = response_queue
        self.emergency_queue = emergency_queue
        self.nc = NATS()
        self.js = self.nc.jetstream()
        self.processing = True  # Controls whether work_queue is being processed

    async def connect(self) -> None:
        await self.nc.connect(servers=[self.nats_url])
        print("Connected to NATS")

    async def start(self) -> None:
        # Subscribe to work_queue
        await self.nc.subscribe(self.work_queue, cb=self.process_message)
        print(f"Listening on {self.work_queue}...")

        # Subscribe to emergency_queue
        await self.nc.subscribe(self.emergency_queue, cb=self.handle_emergency)
        print(f"Listening on {self.emergency_queue} for stop/resume commands...")

    async def process_message(self, msg: Msg) -> None:
        if not self.processing:
            print("Paused. Ignoring messages.")
            return

        data = msg.data.decode()
        print(data)
        response = f"Processed: {data}"
        # Publish response to response_queue
        await self.nc.publish(self.response_queue, response.encode())

    async def handle_emergency(self, msg: Msg) -> None:
        command = msg.data.decode().lower()
        if command == "stop":
            self.processing = False
            print("Received STOP command. Pausing message processing.")
        elif command == "resume":
            self.processing = True
            print("Received RESUME command. Resuming message processing.")
        else:
            print(f"Unknown command on {self.emergency_queue}: {command}")

    async def close(self) -> None:
        await self.nc.close()
        print("Disconnected from NATS")


async def publish_messages(nc: NATS, work_queue: str, emergency_queue: str) -> None:
    count = 0
    while True:
        # Publish a random message to the work_queue
        msg = RobotMessage(command=RobotCommand(
            command="HOME",
        ))
        await nc.publish(work_queue, msg.model_dump_json().encode())
        msg = RobotMessage(command=RobotCommand(
            command="STOP",
        ))
        await nc.publish(work_queue, msg.model_dump_json().encode())
        count += 1

        # Randomly send stop or resume commands to the emergency_queue
        if random.random() < 0.2:  # 20% chance of sending a command
            command = random.choice(["stop", "resume"])
            await nc.publish(emergency_queue, command.encode())
        await asyncio.sleep(2)  # Delay between messages


async def main() -> None:
    # Configuration
    nats_url = "nats://localhost:4222"
    work_queue = "work_queue"
    response_queue = "response_queue"
    emergency_queue = "emergency_queue"

    # Initialize Robot
    robot = RobotWorker(
        nats_url=nats_url,
        work_queue=work_queue,
        response_queue=response_queue,
        emergency_queue=emergency_queue,
    )

    # Connect to NATS
    await robot.connect()

    # Start the worker and publisher concurrently
    await asyncio.gather(
        robot.start(),
        publish_messages(robot.nc, work_queue, emergency_queue),
    )

    # Close connection on exit
    await robot.close()
"""

class RobotClient(AbstractRobotClient):
    def __init__(self, command_queue: str, emergency_command_queue: str):
        self._nc = NATS()
        self._js = self._nc.jetstream()
        self._command_queue = command_queue 
        self._emergency_command_queue = emergency_command_queue

    async def send_command(self, command: RobotCommands) -> None:
        print(f"Command queued: {command}")
        await self._nc.publish(self._command_queue, command.model_dump_json().encode())

    async def send_emergency_command(self, command: RobotCommands) -> None:
        print(f"Command queued: {command}")
        await self._nc.publish(self._emergency_command_queue, command.model_dump_json().encode())

    async def get_queued_commands(self) -> List[RobotCommands]:
        ...

    async def handle_response(self, response: RobotResponses) -> None:
        ...


class RobotWorker(AbstractRobotWorker):
    _instance: Optional["RobotWorker"] = None

    def __init__(self, robot: P0C):
        if RobotWorker._instance is not None:
            raise RuntimeError("RobotWorker is a singleton. Use `get_instance()` to access it.")

        # Shared queues
        self._command_queue: asyncio.Queue[RobotCommands] = asyncio.Queue()

        # Client shares the same queues
        self.client = RobotClient("command_queue", "emergency_queue")
        self._robot = robot 
        self._current_command: Optional[RobotCommands] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self.running = False

    @classmethod
    async def get_instance(cls, bus: CANBusHandler, configuration: RobotConfiguration) -> "RobotWorker":
        if cls._instance is None:
            robot = await P0C.build(bus, configuration)
            #await robot.initialize()
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
                    #await self._robot.handle_command(command)
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

