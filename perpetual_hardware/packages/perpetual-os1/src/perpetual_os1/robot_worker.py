import asyncio
from datetime import datetime
from typing import Optional, List
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats import errors as nats_errors
from nats.js.errors import NotFoundError
from pydantic import BaseModel, TypeAdapter, Field
from perpetual_os1.robot import (
    RobotConfiguration,
    Robot,
    RobotCommands,
    RobotResponse,
    RobotStatus,
    StopCommand,
)
from perpetual_os1.storage import StorageGrid
from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1.robot_client import RobotClient


class WorkerError(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    error_type: str
    message: str
    context: Optional[dict] = None
    stack_trace: Optional[str] = None


class WorkerStatus(BaseModel):
    robot_status: Optional[RobotStatus]
    running: bool
    current_command: Optional[RobotCommands]
    error_queue: List[WorkerError] = Field(default_factory=list)


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
        self._error_queue: List[WorkerError] = []

    def get_client(self) -> RobotClient:
        return self._client

    def _add_error(self, error: WorkerError) -> None:
        """Add error to queue, maintaining max size of 10"""
        if len(self._error_queue) >= 10:
            self._error_queue.pop(0)
        self._error_queue.append(error)

    async def _publish_status_with_errors(self, current_command: Optional[RobotCommands] = None) -> None:
        """Publish status including error queue"""
        try:
            robot_status = await self._robot.status()
        except Exception as e:
            robot_status = None
            self._add_error(WorkerError(
                error_type="StatusRetrievalError",
                message=f"Failed to get robot status: {str(e)}",
                context={"command": current_command.model_dump() if current_command else None}
            ))
        
        status = WorkerStatus(
            robot_status=robot_status,
            running=self._running,
            current_command=current_command or self._current_command,
            error_queue=self._error_queue.copy()
        )
        await self._nc.publish(
            self._nats_config.status_queue,
            status.model_dump_json().encode()
        )

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
                        await self._publish_status_with_errors(command)
                        self._current_task = asyncio.create_task(
                            self._robot.handle_command(command)
                        )
                else:
                    if self._current_task.done():
                        try:
                            result: RobotResponse = self._current_task.result()
                            await self._nc.publish(
                                self._nats_config.event_queue, result.model_dump_json().encode()
                            )
                        except Exception as e:
                            self._add_error(WorkerError(
                                error_type="CommandExecutionError",
                                message=f"Command failed: {str(e)}",
                                context={"command": self._current_command.model_dump() if self._current_command else None},
                                stack_trace=str(e.__traceback__) if hasattr(e, '__traceback__') else None
                            ))
                            await self._publish_status_with_errors()
                        
                        if self._msg:
                            await self._msg.ack()
                        self._current_task = None
                        self._current_command = None
                        self._msg = None

                await asyncio.sleep(0.1)
            except (nats_errors.TimeoutError, asyncio.TimeoutError, asyncio.CancelledError) as e:
                await asyncio.sleep(0.1)
                if isinstance(e, (nats_errors.TimeoutError, asyncio.TimeoutError)):
                    self._add_error(WorkerError(
                        error_type="TimeoutError",
                        message="Operation timed out",
                        context={"command": self._current_command.model_dump() if self._current_command else None}
                    ))
                if self._msg:
                    await self._msg.ack()
                await self._publish_status_with_errors()
                self._current_task = None
                self._current_command = None
                self._msg = None
            #except Exception as e:
            #    robot_status = await self._robot.status()
            #    error_status = WorkerStatus(
            #        robot_status=robot_status, running=True, current_command=None
            #    )
            #    await self._nc.publish(
            #        self._nats_config.event_queue, error_status.model_dump_json().encode()
            #    )
            #    if self._msg:
            #        await self._msg.ack()
            #    self._running = False
            #    self._current_task = None
            #    self._current_command = None
            #    self._msg = None

            except Exception as e:
                print(f"Unexpected exception: {e}")
                import traceback
                self._add_error(WorkerError(
                    error_type="UnexpectedError",
                    message=f"Unexpected error: {str(e)}",
                    context={"command": self._current_command.model_dump() if self._current_command else None},
                    stack_trace=traceback.format_exc()
                ))
                
                try:
                    await self._publish_status_with_errors()
                except Exception as pub_err:
                    print(f"Error publishing error status: {pub_err}")

                if self._msg:
                    try:
                        await self._msg.ack()
                    except Exception as ack_err:
                        print(f"Failed to ack msg: {ack_err}")

                self._current_task = None
                self._current_command = None
                self._msg = None

                await asyncio.sleep(1)

    async def start(self) -> None:
        if not self._running:
            self._running = True
            if self._worker_task:
                self._worker_task.cancel()
            self._worker_task = asyncio.create_task(self.process_commands())
            await self._publish_status_with_errors()

    async def shutdown(self) -> None:
        print("Shutting Down")
        #if self._running:
        self._running = False
        if self._worker_task:
            print("Cancel Worker Task")
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        # Send a stop command before shutting down.

        print("Send Stop Command")
        try:
            await self._robot.handle_command(StopCommand())
        except Exception as e:
            self._add_error(WorkerError(
                error_type="ShutdownError",
                message=f"Error during stop command: {str(e)}",
                context={"phase": "stop_command"}
            ))
        
        print("Send Shutdown")
        try:
            await self._robot.shutdown()
        except Exception as e:
            self._add_error(WorkerError(
                error_type="ShutdownError",
                message=f"Error during robot shutdown: {str(e)}",
                context={"phase": "robot_shutdown"}
            ))
        
        print("Publish Status ")
        self._running = False
        await self._publish_status_with_errors()

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
        try:
            robot_status = await self._robot.status()
        except:
            robot_status = None
        
        status = WorkerStatus(
            robot_status=robot_status,
            running=self._running,
            current_command=self._current_command,
            error_queue=self._error_queue.copy()
        )
        await self._nc.publish(msg.reply, status.model_dump_json().encode())
