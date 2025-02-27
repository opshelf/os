from nats.aio.msg import Msg
from nats import errors as nats_errors
import logging
import asyncio
from typing import Optional
from nats.aio.client import Client as NATS
from pydantic import BaseModel, TypeAdapter, ValidationError
from perpetual_p0c_server.core.events import InputDoorEvent, OutputDoorEvent, WorkstationEvents
from perpetual_p0c_server.settings import get_settings
from .base import AbstractWorkstation


class WorkstationNatsConfiguration(BaseModel):
    stream_name: str = "workstation"
    command_queue: str = "commands"
    event_queue: str = "events"
    status_queue: str = "workstation.status"


NATS_CONFIG = WorkstationNatsConfiguration()

WorkstationEventAdapter = TypeAdapter[WorkstationEvents](WorkstationEvents)


class WorkstationClient:
    def __init__(self, nats_config: WorkstationNatsConfiguration):
        self._nc = NATS()
        self._nats_config = nats_config
        self._js = self._nc.jetstream()

    async def connect(self) -> None:
        await self._nc.connect(servers=[get_settings().nats.connection_string()])
        self._js = self._nc.jetstream()

    async def send_command(self, command: WorkstationEvents) -> None:
        if self._js:
            await self._js.publish(
                f"{self._nats_config.stream_name}_{self._nats_config.command_queue}", command.model_dump_json().encode()
            )
        else:
            raise Exception("Client Connection Never Initialized")


class WorkstationWorker:
    _instance: Optional["WorkstationWorker"] = None

    def __init__(
        self,
        workstation: AbstractWorkstation,
        nats_config: WorkstationNatsConfiguration = WorkstationNatsConfiguration(),
    ):
        if WorkstationWorker._instance is not None:
            raise RuntimeError(
                "WorkstationWorker is a singleton. Use `get_instance()` to access it."
            )
        self._command_queue: asyncio.Queue[WorkstationEvents] = asyncio.Queue()
        self._client = WorkstationClient(nats_config)
        self._workstation = workstation
        self._current_command: Optional[WorkstationEvents] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._running = False
        self._nats_config = nats_config
        self._nc = NATS()
        self._msg: Optional[Msg] = None

    async def connect(self) -> None:
        """Connect to the NATS server."""
        await self._nc.connect(servers=[get_settings().nats.connection_string()])
        self._js = self._nc.jetstream()
        await self._js.add_stream(
            name=self._nats_config.stream_name,
            subjects=[
                f"{self._nats_config.stream_name}.{self._nats_config.command_queue}",
                f"{self._nats_config.stream_name}.{self._nats_config.event_queue}",
            ],
        )
        self.work_consumer = await self._js.pull_subscribe(
            f"{self._nats_config.stream_name}_{self._nats_config.command_queue}",
            durable=f"{self._nats_config.stream_name}_{self._nats_config.command_queue}",
        )
        await self._client.connect()

    @classmethod
    async def get_instance(cls, workstation: AbstractWorkstation) -> "WorkstationWorker":
        if cls._instance is None:
            cls._instance = cls(workstation)
        return cls._instance

    async def get_client(self) -> WorkstationClient:
        return self._client

    async def process_commands(self) -> None:
        try:
            while self._running:
                try:
                    if self._current_task is None:
                        msg = await self.work_consumer.fetch(timeout=1)
                        self._msg = msg[0]
                        print(msg[0].data.decode())
                        cmd = WorkstationEventAdapter.validate_json(msg[0].data.decode())
                        logging.info(cmd)
                        self._current_task = asyncio.create_task(
                            self._workstation.handle_command(cmd)
                        )
                        self._current_command = cmd
                    else:
                        if self._current_task and self._current_task.done():
                            match self._current_command:
                                case InputDoorEvent():
                                    pass
                                case OutputDoorEvent():
                                    pass
                                case _:
                                    if self._current_command:
                                        pass
                                    else:
                                        raise Exception("Command Should Always be set")
                            self._current_task = None
                            self._current_command = None
                            if self._msg:
                                await self._msg.ack()
                                self._msg = None
                    await asyncio.sleep(0.05)
                except (nats_errors.TimeoutError, asyncio.CancelledError, ValidationError) as e:
                    if self._msg:
                        await self._msg.ack()
                    self._msg = None
                    self._current_task = None
        except Exception as e:
            logging.error("Process Commands Error: ", e)
            if self._msg:
                await self._msg.ack()
            self._running = False
            self._current_task = None
            self._msg = None

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.process_commands())

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                await self._worker_task
            print("WorkstationWorker shut down.")
