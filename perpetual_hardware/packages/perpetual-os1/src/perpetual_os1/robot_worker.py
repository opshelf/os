import asyncio
from typing import Optional
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats import errors as nats_errors
from pydantic import TypeAdapter
from perpetual_os1.robot import (
    RobotConfiguration,
    Robot,
    RobotCommands,
    RobotResponse,
    StopCommand,
    WorkerStatus
)
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.storage import StorageGrid
from perpetual_os1.common import RobotNatsConfiguration

RobotCommandAdapter = TypeAdapter[RobotCommands](RobotCommands)

class RobotWorker:
    _instance: Optional["RobotWorker"] = None

    def __init__(
        self, robot: Robot, nats_config: RobotNatsConfiguration = RobotNatsConfiguration()
    ):
        if RobotWorker._instance is not None:
            raise RuntimeError("RobotWorker is a singleton. Use `get_instance()` to access it.")
        self._robot = robot
        self._worker_task: Optional[asyncio.Task] = None
        self._current_task: Optional[asyncio.Task] = None
        self._current_command: Optional[RobotCommands] = None
        self._msg: Optional[Msg] = None
        self._running = False
        self._nats_config = nats_config
        self._client = RobotClient(nats_config)
        self._nc = NATS()

    def get_client(self) -> RobotClient:
        return self._client

    async def connect(self) -> None:
        await self._nc.connect(servers=[self._nats_config.connection_string()])
        self._js = self._nc.jetstream()
        await self._js.add_stream(
            name=self._nats_config.stream_name,
            subjects=[
                self._nats_config.command_queue,
                self._nats_config.event_queue,
                self._nats_config.get_storage_subject,
                self._nats_config.set_storage_subject,
                self._nats_config.get_config_subject,
                self._nats_config.set_config_subject,
                self._nats_config.get_status_subject,
            ],
        )
        await self._client.connect()
    
        self.work_consumer = await self._js.pull_subscribe(
            self._nats_config.command_queue, durable=self._nats_config.command_queue
        )
    
        # Register request-reply handlers
        await self._nc.subscribe(self._nats_config.get_storage_subject, cb=self._handle_get_storage)
        await self._nc.subscribe(self._nats_config.set_storage_subject, cb=self._handle_set_storage)
        await self._nc.subscribe(self._nats_config.get_config_subject, cb=self._handle_get_config)
        await self._nc.subscribe(self._nats_config.set_config_subject, cb=self._handle_set_config)
        await self._nc.subscribe(self._nats_config.get_status_subject, cb=self._handle_get_status)


    @classmethod
    async def get_instance(cls, robot: Robot) -> "RobotWorker":
        if cls._instance is None:
            cls._instance = cls(robot)
        return cls._instance

    async def clear_commands(self) -> None:
        if self._js:
            await self._js.purge_stream(self._nats_config.stream_name)

    async def get_storage(self) -> StorageGrid:
        return self._robot.get_storage()

    async def set_storage(self, storage: StorageGrid) -> None:
        self._robot.set_storage(storage)

    async def set_configuration(self, configuration: RobotConfiguration) -> None:
        await self._robot.set_configuration(configuration)

    def get_configuration(self) -> RobotConfiguration:
        return self._robot.configuration

    async def process_commands(self) -> None:
        while self._running:
            try:
                if self._current_task is None:
                    msgs = await self.work_consumer.fetch(timeout=1)
                    if msgs:
                        msg = msgs[0]
                        command = RobotCommandAdapter.validate_json(msg.data.decode())
                        self._current_command = command
                        self._msg = msg
                        status = WorkerStatus(
                            robot_status=None,
                            running=True,
                            current_command=command
                        )
                        await self._nc.publish(
                            self._nats_config.status_queue,
                            status.model_dump_json().encode()
                        )
                        self._current_task = asyncio.create_task(
                            self._robot.handle_command(command)
                        )
                else:
                    if self._current_task.done():
                        result: RobotResponse = self._current_task.result()
                        await self._nc.publish(
                            self._nats_config.event_queue,
                            result.model_dump_json().encode()
                        )
                        if self._msg:
                            await self._msg.ack()
                        self._current_task = None
                        self._current_command = None
                        self._msg = None

                await asyncio.sleep(0.05)
            except (nats_errors.TimeoutError, asyncio.TimeoutError, asyncio.CancelledError) as e:
                if self._msg:
                    await self._msg.ack()
                status = WorkerStatus(robot_status=None, running=True, current_command=None)
                await self._nc.publish(
                    self._nats_config.status_queue,
                    status.model_dump_json().encode()
                )
                self._current_task = None
                self._current_command = None
                self._msg = None
            except Exception as e:
                error_status = WorkerStatus(robot_status=None, running=True, current_command=None)
                await self._nc.publish(
                    self._nats_config.event_queue,
                    error_status.model_dump_json().encode()
                )
                if self._msg:
                    await self._msg.ack()
                self._running = False
                self._current_task = None
                self._current_command = None
                self._msg = None

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.process_commands())
            status = WorkerStatus(robot_status=None, running=True, current_command=None)
            await self._nc.publish(
                self._nats_config.status_queue,
                status.model_dump_json().encode()
            )

    async def shutdown(self) -> None:
        if self._running:
            self._running = False
            if self._worker_task:
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except asyncio.CancelledError:
                    pass
            # Send a stop command before shutting down.
            await self._robot.handle_command(StopCommand())
            shutdown_status = WorkerStatus(robot_status=None, running=False, current_command=None)
            await self._nc.publish(
                self._nats_config.status_queue,
                shutdown_status.model_dump_json().encode()
            )

    async def _handle_get_storage(self, msg: Msg) -> None:
        storage = await self.get_storage()
        await self._nc.publish(msg.reply, storage.model_dump_json().encode())
    
    async def _handle_set_storage(self, msg: Msg) -> None:
        try:
            storage = StorageGrid.model_validate_json(msg.data)
            await self.set_storage(storage)
            await self._nc.publish(msg.reply, b"ok")
        except Exception as e:
            await self._nc.publish(msg.reply, f"error: {e}".encode())
    
    async def _handle_get_config(self, msg: Msg) -> None:
        config = self.get_configuration()
        await self._nc.publish(msg.reply, config.model_dump_json().encode())
    
    async def _handle_set_config(self, msg: Msg) -> None:
        try:
            config = RobotConfiguration.model_validate_json(msg.data)
            await self.set_configuration(config)
            await self._nc.publish(msg.reply, b"ok")
        except Exception as e:
            await self._nc.publish(msg.reply, f"error: {e}".encode())
    
    async def _handle_get_status(self, msg: Msg) -> None:
        status = WorkerStatus(
            robot_status=None,  # Optionally fill in real robot status
            running=self._running,
            current_command=self._current_command,
        )
        await self._nc.publish(msg.reply, status.model_dump_json().encode())
    
