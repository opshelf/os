import asyncio
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict

import sentry_sdk
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg
from nats import errors as nats_errors
from nats.js.errors import NotFoundError
from pydantic import TypeAdapter

from perpetual_os1.robot import (
    Robot,
    RobotCommands,
    RobotResponse,
    RobotCommandSuccessful,
    StopCommand,
    HomeCommand,
)
from perpetual_os1.common import RobotNatsConfiguration
from perpetual_os1.robot_defs import WorkerError, QueuedOperation, WorkerStatus, WorkerStatusWithQueue
from perpetual_os1.robot_client import RobotClient
from perpetual_os1.barcode_protocol import BarcodeService
from perpetual_os1.exceptions import MotorError, MovementError, ToolError

logger = logging.getLogger(__name__)

COMMAND_TIMEOUT_SECONDS = 300

RobotCommandAdapter = TypeAdapter[RobotCommands](RobotCommands)


def _default_axis_status() -> Dict[str, Any]:
    return {
        "status_flags": {
            "value": 0,
            "stall_guard2_status": False,
            "over_temperature": False,
            "over_temperature_pre_warning": False,
            "short_to_ground_a": False,
            "short_to_ground_b": False,
            "open_load_a": False,
            "open_load_b": False,
            "stand_still": False,
        },
        "error_flags": {
            "value": 0,
            "stall_guard_error": False,
            "deviation_error": False,
        },
        "position": 0,
        "homed": 0,
        "hard_stopped": 0,
    }


def _normalize_location(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None

    side = payload.get("side")
    shelf_idx = payload.get("shelf_idx")
    cell_idx = payload.get("cell_idx")

    if side is None or shelf_idx is None or cell_idx is None:
        return None

    try:
        shelf_idx_int = int(shelf_idx)
        cell_idx_int = int(cell_idx)
    except (TypeError, ValueError):
        return None

    return {"side": str(side), "shelf_idx": shelf_idx_int, "cell_idx": cell_idx_int}


def _normalize_command(command: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(command, dict):
        return None

    command_name = command.get("command")
    if command_name not in {"store_item", "retrieve_item"}:
        return None

    location = _normalize_location(command.get("location"))

    if command_name == "retrieve_item" and location is None and command.get("upca") is None:
        return None

    normalized: Dict[str, Any] = {"command": command_name}

    if location is not None:
        normalized["location"] = location

    if command.get("upca") is not None:
        normalized["upca"] = str(command.get("upca"))

    if command_name == "store_item":
        item_payload = command.get("item")
        if isinstance(item_payload, dict):
            normalized["item"] = item_payload

    return normalized


def _transform_worker_status_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    transformed = dict(payload)

    normalized_command = _normalize_command(payload.get("current_command"))
    transformed["current_command"] = normalized_command

    normalized_operations: List[Dict[str, Any]] = []
    for operation in payload.get("queued_operations", []):
        command = _normalize_command(operation.get("command"))
        if command is None:
            continue

        normalized_operations.append(
            {
                "command": command,
                "timestamp": operation.get("timestamp"),
                "location": _normalize_location(operation.get("location")),
                "item_data": operation.get("item_data"),
            }
        )

    transformed["queued_operations"] = normalized_operations
    transformed["queue_length"] = payload.get("queue_length", len(normalized_operations))

    robot_status_payload = payload.get("robot_status") or {}

    state_payload = robot_status_payload.get("state") or {"current_cell": None}
    storage_payload = robot_status_payload.get("storage") or {"left_module": [], "right_module": []}
    curtain_blocked = robot_status_payload.get("curtain_blocked")
    door_open = robot_status_payload.get("door_open")
    curtain_guard_active = robot_status_payload.get("curtain_guard_active")
    awaiting_curtain_ack = robot_status_payload.get("awaiting_curtain_ack", False)
    storage_payload = _normalize_storage_payload(storage_payload)

    transformed["robot_status"] = {
        "gantry": {
            "x_axis": _default_axis_status(),
            "y_axis": _default_axis_status(),
        },
        "tool": {
            "z_axis": _default_axis_status(),
        },
        "state": {
            "current_cell": state_payload.get("current_cell"),
        },
        "storage": storage_payload,
        "curtain_blocked": curtain_blocked,
        "door_open": door_open,
        "curtain_guard_active": curtain_guard_active,
        "awaiting_curtain_ack": awaiting_curtain_ack,
    }

    errors: List[Dict[str, Any]] = []
    for error in payload.get("error_queue", []):
        if isinstance(error, dict):
            errors.append(error)
    transformed["error_queue"] = errors

    transformed["is_crashed"] = payload.get("is_crashed", False)
    transformed["crash_reason"] = payload.get("crash_reason")
    transformed["needs_home"] = payload.get("needs_home", False)
    transformed["needs_home_reason"] = payload.get("needs_home_reason")

    return transformed


def _normalize_storage_payload(storage: Dict[str, Any]) -> Dict[str, Any]:
    def normalize_cell(cell: Dict[str, Any]) -> Dict[str, Any]:
        status = cell.get("status")
        if isinstance(status, str):
            cell = dict(cell)
            cell["status"] = status.lower()
        return cell

    normalized_storage: Dict[str, List[Dict[str, Any]]] = {"left_module": [], "right_module": []}

    for side in ("left_module", "right_module"):
        shelves = storage.get(side) or []
        normalized_shelves: List[Dict[str, Any]] = []
        for shelf in shelves:
            if not isinstance(shelf, dict):
                continue
            cells = shelf.get("cells") or []
            normalized_cells = [normalize_cell(cell) for cell in cells if isinstance(cell, dict)]
            shelf_copy = dict(shelf)
            shelf_copy["cells"] = normalized_cells
            normalized_shelves.append(shelf_copy)
        normalized_storage[side] = normalized_shelves

    return normalized_storage

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
        self._command_started_at: Optional[float] = None
        self._msg: Optional[Msg] = None
        self._running = False
        self._nats_config = nats_config
        self._client = RobotClient(nats_config)
        self._nc = NATS()
        self._js: Any = None  # Will be set in connect()
        self._error_queue: List[WorkerError] = []
        self._queued_commands: List[QueuedOperation] = []
        self._use_enhanced_status = True
        self._barcode: Optional[BarcodeService] = None
        self._barcode_task: Optional[asyncio.Task] = None
        self._latest_status_payload: Optional[str] = None
        self._last_published_curtain: Optional[tuple[Optional[bool], Optional[bool]]] = None
        self._curtain_blocked: Optional[bool] = None
        self._door_open: Optional[bool] = None
        self._curtain_guard_active: Optional[bool] = None
        self._awaiting_curtain_ack: bool = False
        self._guard_lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__).getChild("worker.guard")
        self._is_crashed: bool = False
        self._crash_reason: Optional[str] = None
        self._needs_home: bool = False
        self._needs_home_reason: Optional[str] = None

    def get_client(self) -> RobotClient:
        return self._client

    async def get_scale_data(self) -> Dict[str, Any]:
        return await self._robot.get_scale_data()

    async def get_scale_calibration_status(self) -> Dict[str, Any]:
        return await self._robot.get_scale_calibration_status()

    async def get_scale_calibration_params(self) -> Dict[str, Any]:
        return await self._robot.get_scale_calibration_params()

    async def set_scale_calibration_params(
        self,
        full_scale: int,
        calibration_weight: int,
        precision: int = 1,
        division: int = 1,
    ) -> bool:
        return await self._robot.set_scale_calibration_params(
            full_scale=full_scale,
            calibration_weight=calibration_weight,
            precision=precision,
            division=division,
        )

    async def calibrate_scale_zero(self, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._robot.calibrate_scale_zero(timeout=timeout)

    async def calibrate_scale_weight(self, timeout: float = 5.0) -> Dict[str, Any]:
        return await self._robot.calibrate_scale_weight(timeout=timeout)

    async def scale_tare(self) -> bool:
        return await self._robot.scale_tare()

    def get_scanner_status(self) -> Dict[str, Any]:
        return {
            "connected": self._barcode is not None and self._barcode._transport is not None,
            "enabled": self._should_enable_barcode(),
        }

    def get_motor_names(self) -> Dict[str, List[str]]:
        return self._robot.get_motor_names()

    async def simulate_motor_crash(self, motor_name: str) -> bool:
        success = self._robot.simulate_motor_crash(motor_name)

        if success is True:
            self.set_crash_state(f"Simulated motor fault on {motor_name}")
            self.set_needs_home(f"Simulated motor fault on {motor_name}")
            await self._publish_status()

        return success

    def get_latest_status_payload(self) -> Optional[str]:
        return self._latest_status_payload

    def needs_home(self) -> bool:
        return self._needs_home

    def needs_home_reason(self) -> Optional[str]:
        return self._needs_home_reason

    async def get_status_snapshot(self, force_refresh: bool = False) -> Optional[str]:
        if force_refresh or self._latest_status_payload is None:
            try:
                await self._publish_status()
            except Exception as err:
                logging.warning("Failed to refresh robot status payload: %s", err)
        return self._latest_status_payload

    @classmethod
    async def get_instance(cls, robot: Robot) -> "RobotWorker":
        if cls._instance is None:
            cls._instance = cls(robot)
        return cls._instance

    def _add_error(self, error: WorkerError) -> None:
        if len(self._error_queue) >= 100:
            self._error_queue.pop(0)
        self._error_queue.append(error)

    def _report_error(self, error: WorkerError, exc: Optional[Exception] = None) -> None:
        sentry_event_id: Optional[str] = None
        try:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("error_type", error.error_type)
                if error.context:
                    scope.set_context("worker_error", error.context)
                if self._current_command:
                    scope.set_tag("command", getattr(self._current_command, "command", "unknown"))
                if error.stack_trace:
                    scope.set_extra("stack_trace", error.stack_trace)

                if exc is not None:
                    sentry_event_id = sentry_sdk.capture_exception(exc)
                else:
                    sentry_event_id = sentry_sdk.capture_message(
                        f"{error.error_type}: {error.message}",
                        level="error"
                    )
        except Exception:
            pass

        if sentry_event_id:
            if error.context is None:
                error.context = {}
            error.context["sentry_event_id"] = sentry_event_id

        self._add_error(error)

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

        self._robot.register_curtain_listener(self._on_curtain_change)

        # Curtain change publishing is driven by Robot.get_status() reads when status is requested.

    async def clear_commands(self) -> None:
        if self._js:
            await self._js.purge_stream(self._nats_config.stream_name)
        self._queued_commands = []

    def clear_errors(self) -> None:
        self._error_queue = []

    def clear_crash_state(self) -> None:
        self._is_crashed = False
        self._crash_reason = None
        self._robot.resume_after_curtain()
        self.clear_needs_home()

    def set_crash_state(self, reason: str) -> None:
        self._is_crashed = True
        self._crash_reason = reason

    def clear_needs_home(self) -> None:
        self._needs_home = False
        self._needs_home_reason = None

    def set_needs_home(self, reason: str) -> None:
        self._needs_home = True
        self._needs_home_reason = reason

    async def emergency_stop(self, reason: str) -> None:
        self._robot.pause_for_curtain()
        self.set_crash_state(reason)
        await self._publish_status()
        logging.info("Emergency stop triggered: %s", reason)

        async def _cleanup_after_stop() -> None:
            if self._current_task is not None and not self._current_task.done():
                self._current_task.cancel()
                try:
                    await self._current_task
                except asyncio.CancelledError:
                    pass

            if self._msg is not None:
                try:
                    await self._msg.ack()
                except Exception:
                    pass

            if self._current_command is not None:
                self._queued_commands = [
                    op for op in self._queued_commands
                    if op.command != self._current_command
                ]

            self._current_task = None
            self._current_command = None
            self._command_started_at = None
            self._msg = None

            await self.clear_commands()
            await self._publish_status()

        asyncio.create_task(_cleanup_after_stop())

    def _should_enable_barcode(self) -> bool:
        return os.getenv("PERPETUAL_DISABLE_BARCODE", "false").lower() not in {"1", "true", "yes"}

    async def _run_barcode_bridge(self) -> None:
        if not self._barcode:
            return
        try:
            async for code in self._barcode:
                logging.info("Received barcode: %s", code)
                try:
                    await self._nc.publish(
                        self._nats_config.event_queue,
                        json.dumps({"kind": "label_scanned", "payload": code}).encode(),
                    )
                except Exception as publish_error:  # pragma: no cover - hardware dependent
                    logging.warning("Barcode event publish error: %s", publish_error)
                    self._report_error(
                        WorkerError(
                            error_type="BarcodePublishError",
                            message=str(publish_error),
                            context={"barcode": code},
                        ),
                        exc=publish_error,
                    )
        except asyncio.CancelledError:
            pass
        except Exception as bridge_error:  # pragma: no cover - hardware dependent
            logging.error("Barcode bridge crashed: %s", bridge_error)
            self._report_error(
                WorkerError(
                    error_type="BarcodeBridgeError",
                    message=str(bridge_error),
                ),
                exc=bridge_error,
            )

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
            if robot_status:
                self._curtain_blocked = robot_status.curtain_blocked
                self._door_open = robot_status.door_open
        except Exception as err:
            robot_status = None
            self._report_error(
                WorkerError(
                    error_type="StatusRetrievalError",
                    message=str(err),
                    context={"command": self._current_command.model_dump() if self._current_command else None},
                ),
                exc=err,
            )
        await self._evaluate_curtain_guard(refresh=False)
        if robot_status:
            robot_status.curtain_guard_active = self._curtain_guard_active
            robot_status.door_open = self._door_open
            robot_status.awaiting_curtain_ack = self._awaiting_curtain_ack

        status: WorkerStatus | WorkerStatusWithQueue

        if self._use_enhanced_status:
            pending_count, queued_ops = await self._get_pending_messages_info()
            status = WorkerStatusWithQueue(
                robot_status=robot_status,
                running=self._running,
                current_command=self._current_command,
                queued_operations=queued_ops[:10],
                queue_length=pending_count,
                error_queue=list(self._error_queue),
                is_crashed=self._is_crashed,
                crash_reason=self._crash_reason,
                needs_home=self._needs_home,
                needs_home_reason=self._needs_home_reason,
            )
        else:
            status = WorkerStatus(
                robot_status=robot_status,
                running=self._running,
                current_command=self._current_command,
                error_queue=list(self._error_queue),
                is_crashed=self._is_crashed,
                crash_reason=self._crash_reason,
                needs_home=self._needs_home,
                needs_home_reason=self._needs_home_reason,
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

        status_payload = _transform_worker_status_payload(status_payload)
        payload_json = json.dumps(status_payload)
        self._latest_status_payload = payload_json

        await self._nc.publish(
            self._nats_config.status_queue,
            payload_json.encode(),
        )
        await self._maybe_publish_curtain_event(
            status_payload.get("robot_status", {}).get("curtain_blocked"),
            status_payload.get("robot_status", {}).get("door_open"),
        )

    async def process_commands(self) -> None:
        logger.info("process_commands loop started, running=%s", self._running)
        loop_count = 0
        while self._running:
            try:
                if self._current_task is None:
                    loop_count += 1
                    if loop_count % 30 == 1:
                        logger.debug("process_commands polling, loop=%d", loop_count)
                    msgs = await self.work_consumer.fetch(timeout=1)
                    if msgs:
                        logger.info("Received %d message(s) from queue", len(msgs))
                        msg = msgs[0]
                        try:
                            command = RobotCommandAdapter.validate_json(msg.data.decode())
                        except Exception as err:
                            await msg.ack()
                            self._report_error(
                                WorkerError(
                                    error_type="UnexpectedError",
                                    message=str(err),
                                    context={"command": None},
                                    stack_trace=traceback.format_exc(),
                                ),
                                exc=err,
                            )
                            continue
                        self._current_command = command
                        self._msg = msg
                        self._queued_commands.append(self._snapshot_command(command))
                        await self._publish_status()

                        if isinstance(command, HomeCommand) and self._is_crashed:
                            self._robot.resume_after_curtain()
                            logging.info("Resumed curtain safety for homing in crash state")

                        logging.info("Starting command execution: %s", command.command)
                        self._command_started_at = time.monotonic()
                        self._current_task = asyncio.create_task(self._robot.handle_command(command))
                else:
                    elapsed = time.monotonic() - (self._command_started_at or time.monotonic())
                    if elapsed > COMMAND_TIMEOUT_SECONDS and not self._current_task.done():
                        logging.error(
                            "Command timeout after %.1f seconds: %s",
                            elapsed,
                            self._current_command.command if self._current_command else "unknown",
                        )
                        self._current_task.cancel()
                        try:
                            await self._current_task
                        except asyncio.CancelledError:
                            pass
                        self._report_error(
                            WorkerError(
                                error_type="CommandTimeout",
                                message=f"Command timed out after {elapsed:.1f} seconds",
                                context={"command": self._current_command.model_dump() if self._current_command else None},
                                stack_trace="",
                            ),
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
                        self._command_started_at = None
                        self._msg = None
                        await self._publish_status()
                        continue

                    if self._current_task.done():
                        logging.info("Command task completed, processing result")
                        try:
                            result: RobotResponse = self._current_task.result()
                            logging.info("Command result: %s", type(result).__name__)

                            if (
                                isinstance(self._current_command, HomeCommand)
                                and isinstance(result, RobotCommandSuccessful)
                            ):
                                if self._is_crashed:
                                    self.clear_crash_state()
                                    logging.info("Crash state cleared after successful homing")
                                elif self._needs_home:
                                    self.clear_needs_home()
                                    logging.info("Needs-home flag cleared after successful homing")

                            await self._nc.publish(
                                self._nats_config.event_queue,
                                result.model_dump_json().encode(),
                            )
                            logging.info("Published command result to event queue")
                        except Exception as err:
                            logging.error("Command execution error: %s", err)

                            is_hardware_fault = isinstance(err, (MotorError, MovementError, ToolError))
                            if is_hardware_fault:
                                self.set_crash_state(str(err))
                                self.set_needs_home(str(err))
                                logging.warning("Hardware fault detected - robot in crash state: %s", type(err).__name__)

                            self._report_error(
                                WorkerError(
                                    error_type="HardwareFault" if is_hardware_fault else "CommandExecutionError",
                                    message=str(err),
                                    context={"command": self._current_command.model_dump() if self._current_command else None},
                                    stack_trace=traceback.format_exc(),
                                ),
                                exc=err,
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
                        self._command_started_at = None
                        self._msg = None
                        await self._publish_status()

                await asyncio.sleep(0.1)
            except (nats_errors.TimeoutError, asyncio.TimeoutError) as timeout_err:
                if self._current_task is not None and not self._current_task.done():
                    logging.warning("Cancelling orphaned command task due to NATS timeout")
                    self._current_task.cancel()
                    try:
                        await self._current_task
                    except asyncio.CancelledError:
                        pass
                    self._report_error(
                        WorkerError(
                            error_type="NATSTimeout",
                            message=f"NATS timeout interrupted command: {timeout_err}",
                            context={"command": self._current_command.model_dump() if self._current_command else None},
                            stack_trace="",
                        ),
                    )
                if self._msg:
                    try:
                        await self._msg.ack()
                    except Exception:
                        pass
                self._current_task = None
                self._current_command = None
                self._command_started_at = None
                self._msg = None
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logging.exception("Unexpected robot worker error: %s", err)
                self._report_error(
                    WorkerError(
                        error_type="UnexpectedError",
                        message=str(err),
                        context={"command": self._current_command.model_dump() if self._current_command else None},
                        stack_trace=traceback.format_exc(),
                    ),
                    exc=err,
                )
                if self._current_task is not None and not self._current_task.done():
                    logging.warning("Cancelling command task due to unexpected error")
                    self._current_task.cancel()
                    try:
                        await self._current_task
                    except asyncio.CancelledError:
                        pass
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
                self._command_started_at = None
                self._msg = None
                await asyncio.sleep(1)

    async def start(self) -> None:
        logger.info("RobotWorker.start() called, _running=%s", self._running)
        if self._running:
            logger.info("RobotWorker already running, returning early")
            return
        self._running = True
        if self._worker_task:
            self._worker_task.cancel()
        logger.info("Creating process_commands task")
        self._worker_task = asyncio.create_task(self.process_commands())
        logger.info("Task created: %s", self._worker_task)
        self._queued_commands = []
        await self._publish_status()
        logger.info("RobotWorker.start() complete")

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
            self._report_error(
                WorkerError(
                    error_type="ShutdownError",
                    message=str(err),
                    context={"phase": "stop_command"},
                    stack_trace=traceback.format_exc(),
                ),
                exc=err,
            )

        try:
            await self._robot.shutdown()
        except Exception as err:
            self._report_error(
                WorkerError(
                    error_type="ShutdownError",
                    message=str(err),
                    context={"phase": "robot_shutdown"},
                    stack_trace=traceback.format_exc(),
                ),
                exc=err,
            )

        await self._publish_status()

        try:
            sentry_sdk.flush(timeout=2.0)
        except Exception:
            pass

    async def _maybe_publish_curtain_event(
        self, blocked: Optional[bool], door_open: Optional[bool]
    ) -> None:
        if (blocked, door_open) == self._last_published_curtain:
            return
        self._last_published_curtain = (blocked, door_open)
        guard_active = bool(blocked) and bool(door_open)
        try:
            payload = json.dumps(
                {
                    "kind": "curtain_state",
                    "payload": {
                        "blocked": blocked,
                        "door_open": door_open,
                        "guard_active": guard_active,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }
            ).encode()
            await self._nc.publish(self._nats_config.event_queue, payload)
            self._logger.debug(
                "Curtain event published",
                extra={
                    "blocked": blocked,
                    "door_open": door_open,
                    "guard_active": guard_active,
                },
            )
        except Exception as err:  # pragma: no cover - observability
            logging.warning("Failed to publish curtain event: %s", err)

    def _on_curtain_change(self, state: Optional[bool]) -> None:
        # Fire-and-forget: publish status and curtain event when curtain changes.
        async def _handle() -> None:
            self._logger.debug(
                "Curtain sensor change received",
                extra={
                    "blocked": state,
                    "previous": self._curtain_blocked,
                    "door_open_cached": self._door_open,
                },
            )
            self._curtain_blocked = state
            await self._evaluate_curtain_guard()
            await self._publish_status()
        asyncio.create_task(_handle())

    async def _evaluate_curtain_guard(self, *, refresh: bool = True) -> None:
        async with self._guard_lock:
            previous_door = self._door_open
            if refresh:
                try:
                    self._door_open = await self._robot.refresh_door_state()
                except Exception as exc:  # pragma: no cover - robustness
                    logging.debug("Door state refresh failed: %s", exc)
            if refresh and self._door_open != previous_door:
                self._logger.debug(
                    "Door state updated for guard evaluation",
                    extra={"door_open": self._door_open, "previous": previous_door},
                )
            # Treat unknown door state as closed: guard only when door is known open.
            guard_active = bool(self._curtain_blocked) and bool(self._door_open)
            guard_changed = guard_active != self._curtain_guard_active
            self._curtain_guard_active = guard_active

            if guard_changed:
                if guard_active:
                    self._robot.pause_for_curtain()
                    self._awaiting_curtain_ack = True
                elif self._awaiting_curtain_ack:
                    self._robot.resume_after_curtain()
                    self._awaiting_curtain_ack = False
                self._logger.info(
                    "Curtain guard change",
                    extra={
                        "curtain_blocked": self._curtain_blocked,
                        "door_open": self._door_open,
                        "guard_active": self._curtain_guard_active,
                        "awaiting_ack": self._awaiting_curtain_ack,
                    },
                )

    async def acknowledge_curtain_guard(self) -> bool:
        async with self._guard_lock:
            if self._curtain_blocked:
                self._logger.info(
                    "Curtain guard ack rejected: curtain still blocked",
                    extra={"curtain_blocked": self._curtain_blocked},
                )
                return False

            if self._awaiting_curtain_ack:
                self._awaiting_curtain_ack = False
                self._robot.resume_after_curtain()
                self._logger.info("Curtain guard acknowledged, resuming operation")

        await self._publish_status()
        return True

    async def release_gripper(self) -> None:
        await self._robot.release_gripper()

    async def grip(self) -> None:
        await self._robot.grip()
