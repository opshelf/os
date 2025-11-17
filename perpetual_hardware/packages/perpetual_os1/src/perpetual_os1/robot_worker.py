import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, List, Any

from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats import errors as nats_errors
from nats.js.errors import NotFoundError
from pydantic import BaseModel, Field, TypeAdapter

from perpetual_os1.robot import (
    Robot,
    RobotCommands,
    RobotResponse,
    RobotStatus,
    StopCommand,
    StoreItemCommand
)
from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1.robot_defs import WorkerError, QueuedOperation, WorkerStatus, WorkerStatusWithQueue
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.barcode_protocol import BarcodeService

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
        self._js = None  # Will be set in connect()
        self._error_queue: List[WorkerError] = []
        self._queued_commands: List[QueuedOperation] = []
        self._use_enhanced_status = True
        self._barcode: Optional[BarcodeService] = None
        self._barcode_task: Optional[asyncio.Task] = None

    def get_client(self) -> RobotClient:
        return self._client

    @classmethod
    async def get_instance(cls, robot: Robot) -> "RobotWorker":
        if cls._instance is None:
            cls._instance = cls(robot)
        return cls._instance

    def _add_error(self, error: WorkerError) -> None:
        if len(self._error_queue) >= 10:
            self._error_queue.pop(0)
        self._error_queue.append(error)

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

        if self._should_enable_barcode() and self._barcode is None:
            # port = os.getenv("PERPETUAL_BARCODE_PORT", "/dev/ttyACM1")
            port = os.getenv("PERPETUAL_BARCODE_PORT", "/dev/serial/by-id/usb-Linux_4.9.84_Barcode_Scanner-if00")
            baudrate = int(os.getenv("PERPETUAL_BARCODE_BAUDRATE", "115200"))
            try:
                self._barcode = BarcodeService(port=port, baudrate=baudrate)
                await self._barcode.start()
                self._barcode_task = asyncio.create_task(
                    self._run_barcode_bridge(),
                    name="barcode-bridge",
                )
                logging.info("Barcode bridge started on %s", port)
            except Exception as exc:  # pragma: no cover - hardware dependent
                logging.warning("Barcode service disabled: %s", exc)
                self._barcode = None
                self._barcode_task = None

    async def clear_commands(self) -> None:
        if self._js:
            await self._js.purge_stream(self._nats_config.stream_name)
        self._queued_commands = []

    def _should_enable_barcode(self) -> bool:
        return os.getenv("PERPETUAL_DISABLE_BARCODE", "false").lower() not in {"1", "true", "yes"}

    async def _run_barcode_bridge(self) -> None:
        if not self._barcode:
            return
        try:
            async for code in self._barcode:
                try:
                    command = await self._barcode_to_command(code)
                except Exception as mapping_error:  # pragma: no cover - hardware dependent
                    logging.warning("Barcode mapping error for %s: %s", code, mapping_error)
                    self._add_error(
                        WorkerError(
                            error_type="BarcodeMappingError",
                            message=str(mapping_error),
                            context={"barcode": code},
                        )
                    )
                    continue

                try:
                    await self._nc.publish(
                        self._nats_config.command_queue,
                        command.model_dump_json().encode(),
                    )
                except Exception as publish_error:  # pragma: no cover - hardware dependent
                    logging.warning("Barcode publish error: %s", publish_error)
                    self._add_error(
                        WorkerError(
                            error_type="BarcodePublishError",
                            message=str(publish_error),
                            context={"barcode": code},
                        )
                    )
        except asyncio.CancelledError:
            pass
        except Exception as bridge_error:  # pragma: no cover - hardware dependent
            logging.error("Barcode bridge crashed: %s", bridge_error)
            self._add_error(
                WorkerError(
                    error_type="BarcodeBridgeError",
                    message=str(bridge_error),
                )
            )

    async def _barcode_to_command(self, code: str) -> RobotCommands:
        logging.info("Received barcode: %s", code)
        pass
        # TODO: map barcodes to real RobotCommands
        # return StoreItemCommand(item={"name":code})

    def _snapshot_command(self, command: RobotCommands) -> QueuedOperation:
        metadata: dict[str, Any] = {}
        for attribute in ("metadata",):
            value = getattr(command, attribute, None)
            if value is not None:
                metadata[attribute] = value

        location = getattr(command, "location", None)
        item_data = getattr(command, "item", None)

        return QueuedOperation(
            command=command,
            timestamp=datetime.now(timezone.utc),
            location=location,
            item_data=item_data,
            metadata=metadata,
        )

    async def _get_pending_messages_info(self) -> tuple[int, List[QueuedOperation]]:
        if self._js is None:
            return 0, []
        try:
            consumer_info = await self._js.consumer_info(
                self._nats_config.stream_name,
                self._nats_config.command_queue,
            )
            return consumer_info.num_pending, list(self._queued_commands)
        except NotFoundError:
            return 0, []
        except Exception as err:  # pragma: no cover - observability
            logging.warning("Failed to fetch consumer info: %s", err)
            return 0, []

    async def _publish_status(self) -> None:
        try:
            robot_status = await self._robot.get_status()
        except Exception as err:
            robot_status = None
            self._add_error(
                WorkerError(
                    error_type="StatusRetrievalError",
                    message=str(err),
                    context={"command": self._current_command.model_dump() if self._current_command else None},
                )
            )

        if self._use_enhanced_status:
            pending_count, queued_ops = await self._get_pending_messages_info()
            status = WorkerStatusWithQueue(
                robot_status=robot_status,
                running=self._running,
                current_command=self._current_command,
                queued_operations=queued_ops[:10],
                queue_length=pending_count,
                error_queue=list(self._error_queue),
            )
        else:
            status = WorkerStatus(
                robot_status=robot_status,
                running=self._running,
                current_command=self._current_command,
                error_queue=list(self._error_queue),
            )

        status_payload = status.model_dump(mode="json")

        if status.error_queue:
            status_payload["error_queue"] = [
                error.model_dump(mode="json", exclude_none=True) for error in status.error_queue
            ]

        if isinstance(status, WorkerStatusWithQueue) and status.queued_operations:
            status_payload["queued_operations"] = [
                op.model_dump(mode="json") for op in status.queued_operations
            ]

        await self._nc.publish(
            self._nats_config.status_queue,
            json.dumps(status_payload).encode(),
        )

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
                        self._queued_commands.append(self._snapshot_command(command))
                        await self._publish_status()
                        self._current_task = asyncio.create_task(self._robot.handle_command(command))
                else:
                    if self._current_task.done():
                        try:
                            result: RobotResponse = self._current_task.result()
                            await self._nc.publish(
                                self._nats_config.event_queue,
                                result.model_dump_json().encode(),
                            )
                        except Exception as err:
                            self._add_error(
                                WorkerError(
                                    error_type="CommandExecutionError",
                                    message=str(err),
                                    context={"command": self._current_command.model_dump() if self._current_command else None},
                                )
                            )

                        if self._msg:
                            await self._msg.ack()

                        if self._current_command:
                            self._queued_commands = [
                                op for op in self._queued_commands
                                if op.command != self._current_command
                            ]

                        self._current_task = None
                        self._current_command = None
                        self._msg = None
                        await self._publish_status()

                await asyncio.sleep(0.1)
            except (nats_errors.TimeoutError, asyncio.TimeoutError):
                if self._msg:
                    await self._msg.ack()
                self._current_task = None
                self._current_command = None
                self._msg = None
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logging.exception("Unexpected robot worker error: %s", err)
                import traceback

                self._add_error(
                    WorkerError(
                        error_type="UnexpectedError",
                        message=str(err),
                        context={"command": self._current_command.model_dump() if self._current_command else None},
                        stack_trace=traceback.format_exc(),
                    )
                )
                try:
                    await self._publish_status()
                except Exception as publish_err:
                    logging.warning("Failed to publish error status: %s", publish_err)

                if self._msg:
                    try:
                        await self._msg.ack()
                    except Exception as ack_err:
                        logging.warning("Failed to ack NATS message: %s", ack_err)

                self._current_task = None
                self._current_command = None
                self._msg = None
                await asyncio.sleep(1)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        if self._worker_task:
            self._worker_task.cancel()
        self._worker_task = asyncio.create_task(self.process_commands())
        self._queued_commands = []
        await self._publish_status()

    async def shutdown(self) -> None:
        logging.info("Shutting down robot worker")
        self._running = False

        if self._barcode_task:
            logging.info("Stopping barcode bridge")
            self._barcode_task.cancel()
            try:
                await self._barcode_task
            except asyncio.CancelledError:
                pass
            self._barcode_task = None
        if self._barcode:
            try:
                await self._barcode.stop()
            except Exception as exc:  # pragma: no cover - hardware dependent
                logging.warning("Error stopping barcode service: %s", exc)
            self._barcode = None

        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

        try:
            await self._robot.handle_command(StopCommand())
        except Exception as err:
            self._add_error(
                WorkerError(
                    error_type="ShutdownError",
                    message=str(err),
                    context={"phase": "stop_command"},
                )
            )

        try:
            await self._robot.shutdown()
        except Exception as err:
            self._add_error(
                WorkerError(
                    error_type="ShutdownError",
                    message=str(err),
                    context={"phase": "robot_shutdown"},
                )
            )

        await self._publish_status()
