from nats.aio.msg import Msg
from nats.js.errors import NotFoundError
from .base import AbstractRobotClient, AbstractRobotWorker
from perpetual_p0c.robot import AbstractRobot, ReturnCurrentTray, StoreCommand, RetrieveCommand
import asyncio
from typing import List, Optional
from perpetual_p0c.robot import RobotCommands, RobotResponses
from nats.aio.client import Client as NATS
from pydantic import BaseModel, TypeAdapter
from nats.errors import TimeoutError
from perpetual_p0c_server.core.storage import get_storage


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

    async def send_command(self, command: RobotCommands) -> None:
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
        consumer_info = await self._js.consumer_info(
            self._nats_config.stream_name, self._nats_config.command_queue
        )
        messages: List[RobotCommands] = []
        # Iterate over all available messages
        adapter = TypeAdapter[RobotCommands](RobotCommands)
        for seq in range(consumer_info.num_pending if consumer_info.num_pending else 0):
            try:
                msg = await self._js.get_msg(self._nats_config.stream_name, seq + 1)
                if msg.data:
                    messages.append(adapter.validate_json(msg.data.decode()))
            except NotFoundError:
                pass
        return messages

    async def handle_response(self, response: RobotResponses) -> None:
        raise NotImplemented


class RobotWorker(AbstractRobotWorker):
    _instance: Optional["RobotWorker"] = None

    def __init__(
        self, robot: AbstractRobot, nats_config: RobotNatsConfiguration = RobotNatsConfiguration()
    ):
        if RobotWorker._instance is not None:
            raise RuntimeError("RobotWorker is a singleton. Use `get_instance()` to access it.")
        self._command_queue: asyncio.Queue[RobotCommands] = asyncio.Queue()
        self._client = RobotClient(nats_config)
        self._robot = robot
        self._current_command: Optional[RobotCommands] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._running = False
        self._nats_config = nats_config
        self._nc = NATS()
        self._msg: Optional[Msg] = None

    async def connect(self) -> None:
        """Connect to the NATS server."""
        await self._nc.connect(servers=[self._nats_config.nats_url])
        self._js = self._nc.jetstream()
        await self._js.add_stream(
            name=self._nats_config.stream_name,
            subjects=[self._nats_config.command_queue, self._nats_config.event_queue],
        )
        self.work_consumer = await self._js.pull_subscribe(
            self._nats_config.command_queue, durable=self._nats_config.command_queue
        )
        await self._client.connect()

    @classmethod
    async def get_instance(cls, robot: AbstractRobot) -> "RobotWorker":
        if cls._instance is None:
            cls._instance = cls(robot)
        return cls._instance

    async def get_client(self) -> AbstractRobotClient:
        return self._client

    async def process_commands(self) -> None:
        adapter = TypeAdapter[RobotCommands](RobotCommands)
        try:
            while self._running:
                try:
                    if self._current_task is None:
                        msg = await self.work_consumer.fetch(timeout=1)
                        # Msg will be a list greater than one or it will give a timeout error
                        cmd = adapter.validate_json(msg[0].data.decode())
                        self._current_task = asyncio.create_task(
                            self._robot.handle_command(cmd)
                        )
                        self._msg = msg[0]
                        self._current_command = cmd
                    else:
                        if self._current_task and self._current_task.done():
                            match self._current_command:
                                case StoreCommand(payload=payload):
                                    storage = get_storage()
                                    storage.store_item(payload.location, payload.item)
                                    storage.save()
                                case RetrieveCommand(payload=payload):
                                    storage = get_storage()
                                    storage.clear_item(payload.location)
                                    storage.save()
                                case _:
                                    pass
                            await self._js.publish(
                                self._nats_config.event_queue,
                                self._current_task.result().model_dump_json().encode(),
                            )
                            self._current_task = None
                            self._current_command = None
                            if self._msg:
                                await self._msg.ack()
                                self._msg = None
                    await asyncio.sleep(0.5)
                except (TimeoutError, asyncio.CancelledError) as e:
                    if self._msg:
                        await self._msg.ack()
                    self._msg = None
                    self._current_task = None
                    #self._robot.handle_command(ReturnCurrentTray())
        except Exception as e:
            print("Process Commands Error: ", e)
            if self._msg:
                await self._msg.ack()
            self._running = False
            self._worker_task = None
            self._msg = None

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
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

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                await self._worker_task
            print("RobotWorker shut down.")

    async def clear_commands(self) -> None:
        if self._js:
            await self._js.purge_stream(self._nats_config.stream_name)
