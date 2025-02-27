from nats.aio.msg import Msg
from nats import errors as nats_errors
from nats.js.errors import NotFoundError
from perpetual_p0c.storage import CellItem, Dimensions, StorageGrid
from perpetual_p0c_server.core.scanner.waveshare import GS1Parser
from perpetual_p0c_server.core.sizes import get_size_db
from .base import AbstractRobotClient, AbstractRobotWorker
import logging
from perpetual_p0c.robot import (
    AbstractRobot,
    RetrievePayload,
    ReturnCurrentTray,
    RobotConfiguration,
    RobotStatus,
    StopCommand,
    StoreCommand,
    RetrieveCommand,
    StorePayload,
)
import asyncio
from typing import List, Optional, Union
from perpetual_p0c.robot import RobotCommands, RobotResponses
from nats.aio.client import Client as NATS
from pydantic import BaseModel, TypeAdapter, ValidationError
from perpetual_p0c_server.core.storage import get_storage
from perpetual_p0c_server.core.events import (
    InputDoorEvent,
    MessagePayload,
    StorageEvents,
    RetrievalRequestedEvent,
    RetrievalFailedEvent,
    RetrievalCompletedEvent,
    StoringRequestedEvent,
    StoringFailedEvent,
    StoringCompletedEvent,
    InputPlacedEvent,
    WorkstationEvents,
)
from perpetual_p0c_server.settings import get_settings


class RobotNatsConfiguration(BaseModel):
    stream_name: str = "robot"
    command_queue: str = "commands"
    event_queue: str = "events"
    status_queue: str = "robot.status"


NATS_CONFIG = RobotNatsConfiguration()

RobotEvents = Union[RobotCommands, StorageEvents]
RobotEventAdapter = TypeAdapter[RobotEvents](RobotEvents)
RobotCommandAdapter = TypeAdapter[RobotCommands](RobotCommands)


class WorkerStatus(BaseModel):
    robot_status: Optional[RobotStatus]
    running: bool
    current_command: Optional[RobotCommands]


class RobotClient(AbstractRobotClient):
    def __init__(self, nats_config: RobotNatsConfiguration):
        self._nc = NATS()
        self._nats_config = nats_config
        self._js = self._nc.jetstream()

    async def connect(self) -> None:
        await self._nc.connect(servers=[get_settings().nats.connection_string()])
        self._js = self._nc.jetstream()

    async def send_command(self, command: RobotEvents) -> None:
        if self._js:
            await self._js.publish(
                self._nats_config.command_queue, command.model_dump_json().encode()
            )
        else:
            raise Exception("Client Connection Never Initialized")

    async def send_emergency_command(self, command: RobotCommands) -> None:
        raise NotImplemented

    async def get_queued_commands(self) -> List[RobotCommands]:
        consumer_info = await self._js.consumer_info(
            self._nats_config.stream_name, self._nats_config.command_queue
        )
        messages: List[RobotCommands] = []
        adapter = TypeAdapter[RobotCommands](RobotCommands)
        for seq in range(consumer_info.num_pending if consumer_info.num_pending else 0):
            try:
                msg = await self._js.get_msg(self._nats_config.stream_name, seq + 1)
                if msg.data:
                    messages.append(adapter.validate_json(msg.data.decode()))
            except NotFoundError:
                pass
        return messages


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
        await self._nc.connect(servers=[get_settings().nats.connection_string()])
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
        try:
            while self._running:
                try:
                    if self._current_task is None:
                        msg = await self.work_consumer.fetch(timeout=1)
                        # Msg will be a list greater than one or it will give a timeout error
                        # cmd = adapter.validate_json(msg[0].data.decode())
                        cmd = RobotEventAdapter.validate_json(msg[0].data.decode())
                        logging.info(cmd)
                        match cmd:
                            case StoringRequestedEvent(payload=payload):
                                size_db = get_size_db()
                                storage = get_storage()
                                gs1 = GS1Parser.parse_gs1_to_model(payload.gs1)
                                item_size = size_db.get_item_size(gs1)
                                if item_size is None:
                                    item_size = Dimensions(length=90.0, width=90.0, height=140.0)
                                item = CellItem(dimensions=item_size, gs1_data=gs1)
                                location = storage.find_best_slot_for_item(item)
                                if location:
                                    store_cmd = StoreCommand(
                                        payload=StorePayload(location=location, item=item)
                                    )
                                    self._current_task = asyncio.create_task(
                                        self._robot.handle_command(store_cmd)
                                    )
                                    self._msg = msg[0]
                                    self._current_command = store_cmd
                                    await self._nc.publish(
                                        self._nats_config.status_queue,
                                        WorkerStatus(
                                            robot_status=None,
                                            running=True,
                                            current_command=store_cmd,
                                        )
                                        .model_dump_json()
                                        .encode(),
                                    )
                                else:
                                    await self._js.publish(
                                        self._nats_config.event_queue,
                                        StoringFailedEvent(
                                            payload=MessagePayload(
                                                message="No Valid Storage Location"
                                            )
                                        )
                                        .model_dump_json()
                                        .encode(),
                                    )
                            case RetrievalRequestedEvent(payload=payload):
                                retrieve = RetrieveCommand(
                                    payload=RetrievePayload(location=payload.location, item=None)
                                )
                                self._current_task = asyncio.create_task(
                                    self._robot.handle_command(retrieve)
                                )
                                self._msg = msg[0]
                                self._current_command = retrieve
                                await self._nc.publish(
                                    self._nats_config.status_queue,
                                    WorkerStatus(
                                        robot_status=None, running=True, current_command=retrieve
                                    )
                                    .model_dump_json()
                                    .encode(),
                                )
                            case RetrieveCommand(payload=payload):
                                self._current_task = asyncio.create_task(
                                    self._robot.handle_command(cmd)
                                )
                                self._msg = msg[0]
                                self._current_command = cmd
                                await self._nc.publish(
                                    self._nats_config.status_queue,
                                    WorkerStatus(
                                        robot_status=None, running=True, current_command=cmd
                                    )
                                    .model_dump_json()
                                    .encode(),
                                )
                            case InputPlacedEvent():
                                pass
                            case StoringCompletedEvent():
                                pass
                            case StoringFailedEvent():
                                pass
                            case RetrievalCompletedEvent():
                                pass
                            case RetrievalFailedEvent():
                                pass
                            case _:
                                self._current_task = asyncio.create_task(
                                    self._robot.handle_command(cmd)
                                )
                                self._current_command = cmd
                                self._msg = msg[0]
                                await self._nc.publish(
                                    self._nats_config.status_queue,
                                    WorkerStatus(
                                        robot_status=None, running=True, current_command=cmd
                                    )
                                    .model_dump_json()
                                    .encode(),
                                )
                    else:
                        if self._current_task and self._current_task.done():
                            match self._current_command:
                                case StoreCommand(payload=payload):
                                    if self._current_task.result():
                                        storage = get_storage()
                                        storage.store_item(payload.location, payload.item)
                                        storage.save()
                                        await self._js.publish(
                                            self._nats_config.event_queue,
                                            StoringCompletedEvent(payload=payload.location)
                                            .model_dump_json()
                                            .encode(),
                                        )
                                    else:
                                        logging.error("Failed Result: ", self._current_task)
                                        await self._js.publish(
                                            self._nats_config.event_queue,
                                            StoringFailedEvent(
                                                payload=MessagePayload(
                                                    message=str("Failed Storage")
                                                )
                                            )
                                            .model_dump_json()
                                            .encode(),
                                        )
                                case RetrieveCommand(payload=payload):
                                    storage = get_storage()
                                    storage.clear_item(payload.location)
                                    storage.save()
                                    response = self._current_task.result()
                                    await self._js.publish(
                                        self._nats_config.event_queue,
                                        RetrievalCompletedEvent(payload={
                                            'output_box': response.output_box
                                        }).model_dump_json().encode(),
                                    )
                                case _:
                                    if self._current_command:
                                        await self._js.publish(
                                            self._nats_config.event_queue,
                                            self._current_task.result().model_dump_json().encode(),
                                        )

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
                    if self._robot.state.current_tray:
                        await self._robot.handle_command(ReturnCurrentTray())
                    self._msg = None
                    self._current_task = None
                    status = await self._robot.status()
                    await self._nc.publish(
                        self._nats_config.status_queue,
                        WorkerStatus(robot_status=status, running=True, current_command=None)
                        .model_dump_json()
                        .encode(),
                    )
        except Exception as e:
            logging.error("Process Commands Error: ", e)
            match self._current_command:
                case StoreCommand(payload=payload):
                    await self._js.publish(
                        self._nats_config.event_queue,
                        StoringFailedEvent(payload=MessagePayload(message=str(e)))
                        .model_dump_json()
                        .encode(),
                    )
                case RetrieveCommand(payload=payload):
                    await self._js.publish(
                        self._nats_config.event_queue,
                        RetrievalFailedEvent(payload=MessagePayload(message=str(e)))
                        .model_dump_json()
                        .encode(),
                    )
            if self._msg:
                await self._msg.ack()
            self._running = False
            self._current_task = None
            await self._robot.handle_command(StopCommand())
            self._msg = None
            self._status = WorkerStatus(robot_status=None, running=False, current_command=None)

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.process_commands())
            self._status = WorkerStatus(robot_status=None, running=True, current_command=None)

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                await self._worker_task
            await self._robot.handle_command(StopCommand())
            print("RobotWorker shut down.")
            self._status = WorkerStatus(robot_status=None, running=False, current_command=None)

    async def clear_commands(self) -> None:
        if self._js:
            await self._js.purge_stream(self._nats_config.stream_name)

    async def get_storage(self) -> StorageGrid:
        return self._robot.storage

    async def set_storage(self, storage: StorageGrid) -> None:
        self._robot.storage = storage

    async def set_configuration(self, configuration: RobotConfiguration) -> None:
        await self._robot.set_configuration(configuration)

    def get_configuration(self) -> RobotConfiguration:
        return self._robot.configuration
