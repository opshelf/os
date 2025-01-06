# from collections.abc import Callable, Coroutine
# from typing import Any
from annotated_types import Not
from nats.js import JetStreamContext
from perpetual_can import CANBusHandler
from .base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c.robot import RobotConfiguration, AbstractRobot
import asyncio
from typing import List, Optional
from perpetual_p0c.robot import RobotCommands, RobotResponses
from nats.aio.client import Client as NATS
from pydantic import BaseModel, TypeAdapter, parse_obj_as
from nats.errors import TimeoutError
import json


class RobotNatsConfiguration(BaseModel):
    nats_url: str = "nats://localhost:4222"
    stream_name: str = "robot"
    command_queue: str = "commmands"
    event_queue: str = "events"


NATS_CONFIG = RobotNatsConfiguration()


class RobotClient(AbstractRobotClient):
    def __init__(self, nats_config: RobotNatsConfiguration):
        self._nc = NATS()
        self._nats_config = nats_config
        self._js = self._nc.jetstream()

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_config.nats_url])
        self._js = self._nc.jetstream()
        print("Client Connected")

    async def send_command(self, command: RobotCommands) -> None:
        print(f"Command queued: {command}")
        if self._js:
            await self._js.publish(
                self._nats_config.command_queue, command.model_dump_json().encode()
            )
        else:
            raise Exception("Client Connection Never Initialized")

    async def send_emergency_command(self, command: RobotCommands) -> None:
        # print(f"Command queued: {command}")
        # await self._nc.publish(self._emergency_command_queue, command.model_dump_json().encode())
        raise NotImplemented

    async def get_queued_commands(self) -> List[RobotCommands]:
        ...
        raise NotImplemented

    async def handle_response(self, response: RobotResponses) -> None:
        ...
        raise NotImplemented


class RobotWorker(AbstractRobotWorker):
    _instance: Optional["RobotWorker"] = None

    def __init__(
        self, robot: AbstractRobot, nats_config: RobotNatsConfiguration = RobotNatsConfiguration()
    ):
        if RobotWorker._instance is not None:
            raise RuntimeError("RobotWorker is a singleton. Use `get_instance()` to access it.")
        # Shared queues
        self._command_queue: asyncio.Queue[RobotCommands] = asyncio.Queue()
        # Client shares the same queues
        self._client = RobotClient(nats_config)
        self._robot = robot
        self._current_command: Optional[RobotCommands] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._running = False
        self._nats_config = nats_config
        self._nc = NATS()

    async def connect(self) -> None:
        """Connect to the NATS server."""
        await self._nc.connect(servers=[self._nats_config.nats_url])
        self.js = self._nc.jetstream()
        await self.js.add_stream(
            name=self._nats_config.stream_name,
            subjects=[self._nats_config.command_queue, self._nats_config.event_queue],
        )
        self.work_consumer = await self.js.pull_subscribe(
            self._nats_config.command_queue, durable=self._nats_config.command_queue
        )
        await self._client.connect()
        print("Connected to NATS")

    @classmethod
    async def get_instance(cls, robot: AbstractRobot) -> "RobotWorker":
        if cls._instance is None:
            cls._instance = cls(robot)
        return cls._instance

    async def get_client(self) -> AbstractRobotClient:
        return self._client

    async def process_commands(self) -> None:
        adapter = TypeAdapter[RobotCommands](RobotCommands)
        while self._running:
            try:
                msg = await self.work_consumer.fetch(timeout=1)
                if len(msg) > 0:
                    cmd = adapter.validate_json(msg[0].data.decode())
                    print(cmd)
                    response = await self._robot.handle_command(cmd)
                    print(response)
                    await self.js.publish(
                        self._nats_config.event_queue, response.model_dump_json().encode()
                    )
                    await msg[0].ack()
                await asyncio.sleep(0.5)
            except (TimeoutError, asyncio.CancelledError) as e:
                pass

    async def start_worker(self) -> None:
        if not self._running:
            self._running = True
            self._worker_task = asyncio.create_task(self.process_commands())
            print("RobotWorker started.")

    async def emergency_command(self, command: RobotCommands) -> None:
        print("Emergency Command: ", command)
        # if self._current_task:
        #    self._current_task.cancel()
        # response = await self._robot.handle_command(command)
        # print(command, response)
        raise NotImplemented

    async def queue_command(self, command: RobotCommands) -> None:
        print("Queue Command: ", command)
        raise NotImplemented
        # await self._command_queue.put(command)

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                await self._worker_task
            print("RobotWorker shut down.")
