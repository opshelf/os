import asyncio
import json
import logging
import os
import re
import time
import traceback
import uuid
import sentry_sdk

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Optional, List, Any, Dict
from openshelf_os.robot.core import Robot
from openshelf_os.robot.commands import (
    RobotCommands,
    StopCommand,
    HomeCommand,
    InitializeCommand,
    RetrieveItemCommand,
    StoreItemCommand,
)
from openshelf_os.robot.status import (
    CabinetDoorState, RobotCommandSuccessful, RobotResponse, SafetyStatus, ScaleStatus,
)
from openshelf_os.robot.models import (
    CrashReportArtifacts,
    CrashReportAttachment,
    QueuedOperation,
    WorkerError,
    WorkerStatusWithQueue,
)
from openshelf_os.barcode_service import (
    DEFAULT_BARCODE_PORT,
    BarcodeService,
    describe_missing_scanner,
)
from openshelf_os.exceptions import MotorError, MovementError, ToolError

logger = logging.getLogger(__name__)

SCALE_OCCUPIED_THRESHOLD_GRAMS = 5.0
SCALE_POLL_INTERVAL_SECONDS = 1.0
SCALE_STATUS_FRESH_SECONDS = 3.0
SCALE_WEIGHT_PUBLISH_DELTA_GRAMS = 1.0
ESTOP_POLL_INTERVAL_SECONDS = 0.05
ESTOP_UNAVAILABLE_REASON = "Emergency stop input unavailable"
PHYSICAL_ESTOP_REASON = "Physical emergency stop triggered"

CrashArtifactProvider = Callable[[str], Awaitable[CrashReportArtifacts]]
CrashArtifactBeginProvider = Callable[[str], Awaitable[CrashReportArtifacts]]
CommandLifecycleCallback = Callable[
    [str, QueuedOperation, Optional[dict[str, Any]]],
    Awaitable[None],
]

_MOTOR_ID_RE = re.compile(r"motor\s+(\d+)", re.IGNORECASE)

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


def _normalize_axis_status(axis_payload: Any) -> Dict[str, Any]:
    normalized = _default_axis_status()

    if isinstance(axis_payload, dict) is False:
        return normalized

    status_flags = axis_payload.get("status_flags")
    if isinstance(status_flags, dict):
        normalized["status_flags"].update(status_flags)

    error_flags = axis_payload.get("error_flags")
    if isinstance(error_flags, dict):
        normalized["error_flags"].update(error_flags)

    if "position" in axis_payload:
        normalized["position"] = axis_payload.get("position", 0)

    if "homed" in axis_payload:
        normalized["homed"] = axis_payload.get("homed", 0)

    if "hard_stopped" in axis_payload:
        normalized["hard_stopped"] = axis_payload.get("hard_stopped", 0)

    return normalized


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

    if command.get("command_id") is not None:
        normalized["command_id"] = str(command["command_id"])
    if command.get("request_id") is not None:
        normalized["request_id"] = str(command["request_id"])
    if command.get("visibility") == "internal":
        normalized["visibility"] = "internal"

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

        normalized_operation = {
            "command": command,
            "command_id": operation.get("command_id"),
            "timestamp": operation.get("timestamp"),
            "location": _normalize_location(operation.get("location")),
            "item_data": operation.get("item_data"),
        }
        if operation.get("visibility") == "internal":
            normalized_operation["visibility"] = "internal"

        normalized_operations.append(normalized_operation)

    transformed["queued_operations"] = normalized_operations
    transformed["queue_length"] = payload.get("queue_length", len(normalized_operations))

    robot_status_payload = payload.get("robot_status") or {}

    state_payload = robot_status_payload.get("state") or {"current_cell": None}
    storage_payload = robot_status_payload.get("storage") or {"left_module": [], "right_module": []}
    cabinet_doors = robot_status_payload.get("cabinet_doors")
    safety_payload = robot_status_payload.get("safety") or {}
    scale_payload = robot_status_payload.get("scale")
    storage_payload = _normalize_storage_payload(storage_payload)

    transformed["robot_status"] = {
        "gantry": {
            "x_axis": _normalize_axis_status(((robot_status_payload.get("gantry") or {}).get("x_axis"))),
            "y_axis": _normalize_axis_status(((robot_status_payload.get("gantry") or {}).get("y_axis"))),
        },
        "tool": {
            "z_axis": _normalize_axis_status(((robot_status_payload.get("tool") or {}).get("z_axis"))),
            "stage": _normalize_axis_status(((robot_status_payload.get("tool") or {}).get("stage"))),
            "rotate": _normalize_axis_status(((robot_status_payload.get("tool") or {}).get("rotate"))),
        },
        "state": {
            "current_cell": state_payload.get("current_cell"),
        },
        "storage": storage_payload,
        "cabinet_doors": cabinet_doors if isinstance(cabinet_doors, dict) else None,
        "io_door_open": robot_status_payload.get("io_door_open"),
        "scale": scale_payload if isinstance(scale_payload, dict) else None,
        "safety": safety_payload if isinstance(safety_payload, dict) else {},
        "motors": {
            axis: _normalize_axis_status(payload)
            for axis, payload in (robot_status_payload.get("motors") or {}).items()
            if isinstance(payload, dict)
        },
    }

    errors: List[Dict[str, Any]] = []
    for error in payload.get("error_queue", []):
        if isinstance(error, dict):
            errors.append(error)
    transformed["error_queue"] = errors

    transformed["is_crashed"] = payload.get("is_crashed", False)
    transformed["crash_reason"] = payload.get("crash_reason")
    transformed["crash_context"] = payload.get("crash_context")
    transformed["is_emergency_stopped"] = payload.get("is_emergency_stopped", False)
    transformed["is_emergency_stop_active"] = payload.get("is_emergency_stop_active", False)
    transformed["emergency_stop_reason"] = payload.get("emergency_stop_reason")
    transformed["needs_home"] = payload.get("needs_home", False)
    transformed["needs_home_reason"] = payload.get("needs_home_reason")
    transformed["needs_home_axes"] = payload.get("needs_home_axes", [])

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
        self,
        robot: Robot,
        crash_artifact_provider: CrashArtifactProvider | None = None,
        crash_artifact_begin_provider: CrashArtifactBeginProvider | None = None,
        command_lifecycle_callback: CommandLifecycleCallback | None = None,
    ):
        if RobotWorker._instance is not None:
            raise RuntimeError("RobotWorker is a singleton. Use `get_instance()` to access it.")

        self._robot = robot
        self._crash_artifact_provider = crash_artifact_provider
        self._crash_artifact_begin_provider = crash_artifact_begin_provider
        self._command_lifecycle_callback = command_lifecycle_callback
        self._worker_task: Optional[asyncio.Task] = None
        self._scale_poll_task: Optional[asyncio.Task] = None
        self._estop_monitor_task: Optional[asyncio.Task] = None
        self._estop_subscription: Any = None
        self._estop_poll_wakeup: Optional[asyncio.Event] = None
        self._current_task: Optional[asyncio.Task] = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._terminalization_lock = asyncio.Lock()
        self._interruption_generation = 0
        self._latest_interruption_reason: Optional[str] = None
        self._current_command: Optional[RobotCommands] = None
        self._current_operation: Optional[QueuedOperation] = None
        self._command_started_at: Optional[float] = None
        self._running = False
        self._command_queue: asyncio.Queue[QueuedOperation] = asyncio.Queue()
        self._error_queue: List[WorkerError] = []
        self._queued_commands: List[QueuedOperation] = []
        self._status_subscribers: List[asyncio.Queue[str]] = []
        self._event_subscribers: List[asyncio.Queue[str]] = []
        self._barcode: Optional[BarcodeService] = None
        self._barcode_task: Optional[asyncio.Task] = None
        self._latest_status_payload: Optional[str] = None
        self._last_published_safety: Optional[str] = None
        self._scale_lock = asyncio.Lock()
        self._latest_scale_status: Optional[ScaleStatus] = None
        self._latest_scale_status_monotonic: Optional[float] = None
        self._last_published_scale_key: Optional[str] = None
        self._curtain_blocked: Optional[bool] = None
        self._door_open: Optional[bool] = None
        self._cabinet_doors: Optional[Dict[str, bool]] = None
        self._safety_active_sources: list[str] = []
        self._safety_sources: dict[str, dict[str, Any]] = {}
        self._safety_motion_blocked: bool = False
        self._awaiting_safety_ack: bool = False
        self._safety_interruption_id: int = 0
        self._safety_last_changed_at: Optional[str] = None
        self._safety_guard_bypassed: bool = False
        self._component_bypasses: dict[str, bool] = {
            "light_curtain": False,
            "cabinet_doors": False,
        }
        self._demo_cycling: bool = False
        self._guard_lock = asyncio.Lock()
        self._logger = logging.getLogger(__name__).getChild("worker.guard")
        self._is_crashed: bool = False
        self._crash_reason: Optional[str] = None
        self._crash_context: Optional[dict[str, Any]] = None
        self._is_emergency_stopped: bool = False
        self._emergency_stop_reason: Optional[str] = None
        self._is_estop_active: bool = False
        self._needs_home_axes: set[str] = set()
        self._needs_home_unknown: bool = False
        self._needs_home_reason: Optional[str] = None

    def _track_background_task(self, coro: Any, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _discard_task(done_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled() is True:
                return

            try:
                done_task.result()
            except Exception as exc:
                logging.warning("Background robot worker task failed: %s", exc, exc_info=True)

        task.add_done_callback(_discard_task)
        return task

    async def _cancel_background_tasks(self) -> None:
        tasks = [task for task in self._background_tasks if task.done() is False]
        if not tasks:
            return

        for task in tasks:
            task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._background_tasks.discard(task)

    def drain_motor_time(self) -> float:
        return self._robot.drain_motor_time()

    async def get_scale_data(self) -> Dict[str, Any]:
        async with self._scale_lock:
            return await self._robot.get_scale_data()

    def get_cached_scale_status(self, max_age_s: float = SCALE_STATUS_FRESH_SECONDS) -> Optional[ScaleStatus]:
        if self._latest_scale_status is None or self._latest_scale_status_monotonic is None:
            return None

        if time.monotonic() - self._latest_scale_status_monotonic > max_age_s:
            return None

        if self._latest_scale_status.available is False:
            return None

        if self._latest_scale_status.stable is False:
            return None

        if self._latest_scale_status.weight_grams is None:
            return None
        return self._latest_scale_status

    async def get_scale_calibration_status(self) -> Dict[str, Any]:
        async with self._scale_lock:
            return await self._robot.get_scale_calibration_status()

    async def get_scale_calibration_params(self) -> Dict[str, Any]:
        async with self._scale_lock:
            return await self._robot.get_scale_calibration_params()

    async def set_scale_calibration_params(
        self,
        full_scale: int,
        calibration_weight: int,
        precision: int = 1,
        division: int = 1,
    ) -> bool:
        async with self._scale_lock:
            result = await self._robot.set_scale_calibration_params(
                full_scale=full_scale,
                calibration_weight=calibration_weight,
                precision=precision,
                division=division,
            )
        if result is True:
            self.invalidate_cached_scale_status()
        return result

    async def calibrate_scale_zero(self, timeout: float = 5.0) -> Dict[str, Any]:
        async with self._scale_lock:
            result = await self._robot.calibrate_scale_zero(timeout=timeout)
        if result.get("success") is True:
            self.invalidate_cached_scale_status()
        return result

    async def calibrate_scale_weight(self, timeout: float = 5.0) -> Dict[str, Any]:
        async with self._scale_lock:
            result = await self._robot.calibrate_scale_weight(timeout=timeout)
        if result.get("success") is True:
            self.invalidate_cached_scale_status()
        return result

    async def scale_tare(self) -> bool:
        async with self._scale_lock:
            result = await self._robot.scale_tare()
        if result is True:
            self.invalidate_cached_scale_status()
        return result

    def invalidate_cached_scale_status(self) -> None:
        """Force the next bin admission to sample the scale after calibration changes."""
        self._latest_scale_status = None
        self._latest_scale_status_monotonic = None

    def _build_scale_status_from_data(self, data: Dict[str, Any]) -> ScaleStatus:
        status_payload = data.get("status") if isinstance(data.get("status"), dict) else {}
        raw_weight = data.get("weight")
        weight_grams: Optional[float] = None

        if raw_weight is not None:
            try:
                weight_grams = abs(float(raw_weight))
            except (TypeError, ValueError):
                weight_grams = None

        decimal_position = data.get("decimal_position")
        if decimal_position is not None:
            try:
                decimal_position = int(decimal_position)
            except (TypeError, ValueError):
                decimal_position = None

        return ScaleStatus(
            enabled=True,
            available=True,
            weight_grams=weight_grams,
            stable=status_payload.get("stable") is True,
            valid=status_payload.get("valid") is True,
            overload=status_payload.get("overload") is True,
            zero=status_payload.get("zero") is True,
            occupied=weight_grams is not None and weight_grams > SCALE_OCCUPIED_THRESHOLD_GRAMS,
            threshold_grams=SCALE_OCCUPIED_THRESHOLD_GRAMS,
            decimal_position=decimal_position,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

    def _build_scale_error_status(self, error: Exception) -> ScaleStatus:
        return ScaleStatus(
            enabled=True,
            available=False,
            occupied=False,
            threshold_grams=SCALE_OCCUPIED_THRESHOLD_GRAMS,
            updated_at=datetime.now(timezone.utc).isoformat(),
            error=str(error),
        )

    def _scale_publish_key(self, scale_status: ScaleStatus) -> str:
        payload = scale_status.model_dump(mode="json")
        payload.pop("updated_at", None)

        weight_grams = scale_status.weight_grams
        payload.pop("weight_grams", None)
        payload["weight_bucket"] = (
            None
            if weight_grams is None
            else round(weight_grams / SCALE_WEIGHT_PUBLISH_DELTA_GRAMS)
        )

        return json.dumps(payload, sort_keys=True)

    async def _refresh_scale_status(self) -> bool:
        try:
            async with self._scale_lock:
                data = await self._robot.get_scale_data()
                scale_status = self._build_scale_status_from_data(data)
                if scale_status.stable is False:
                    return False
                self._latest_scale_status = scale_status
                self._latest_scale_status_monotonic = time.monotonic()
        except Exception as err:
            scale_status = self._build_scale_error_status(err)
            self._latest_scale_status = scale_status
            self._latest_scale_status_monotonic = time.monotonic()
        scale_key = self._scale_publish_key(scale_status)

        if scale_key == self._last_published_scale_key:
            return False

        self._last_published_scale_key = scale_key
        return True

    async def _poll_scale_status(self) -> None:
        while self._running is True:
            try:
                should_publish = await self._refresh_scale_status()
                if should_publish is True:
                    await self._publish_status()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                logging.warning("Scale poll failed: %s", err)

            await asyncio.sleep(SCALE_POLL_INTERVAL_SECONDS)

    def get_scanner_status(self) -> Dict[str, Any]:
        return {
            "connected": self._barcode is not None and self._barcode._transport is not None,
            "enabled": self._should_enable_barcode(),
        }

    async def wait_scanner_connected(self, timeout: float = 2.0) -> bool:
        if self._barcode is None:
            return False

        return await self._barcode.wait_connected(timeout)

    def describe_scanner_failure(self) -> str:
        return describe_missing_scanner()

    def get_motor_names(self) -> Dict[str, List[str]]:
        return self._robot.get_motor_names()

    def get_motor_info(self) -> Dict[str, List[Dict[str, object]]]:
        return self._robot.get_motor_info()

    async def simulate_motor_crash(self, motor_name: str) -> bool:
        success = self._robot.simulate_motor_crash(motor_name)

        if success is True:
            reason = f"Simulated motor fault on {motor_name}"
            fault = RuntimeError(reason)
            self.set_needs_home(reason, axes=[motor_name])
            prepared_artifacts = self._begin_motion_crash_capture(reason)
            try:
                context = await self._build_error_context(fault, is_hardware_fault=True)
            except asyncio.CancelledError:
                self._report_cancelled_motion_crash_handoff(
                    reason=reason,
                    fault=fault,
                    stack_trace="",
                    artifact_capture=prepared_artifacts,
                    report_exception=None,
                )
                raise
            self.set_crash_state(reason, context=context)
            self._queue_motion_crash_report(
                WorkerError(
                    error_type="HardwareFault",
                    message=reason,
                    context=context,
                    stack_trace="",
                ),
                artifact_capture=prepared_artifacts,
            )
            await self._publish_status()

        return success

    def needs_home(self) -> bool:
        return self._needs_home_unknown or bool(self._needs_home_axes)

    def needs_home_reason(self) -> Optional[str]:
        return self._needs_home_reason

    def needs_home_axes(self) -> list[str]:
        return sorted(self._needs_home_axes)

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

    async def _build_error_context(
        self,
        err: Exception,
        *,
        is_hardware_fault: bool,
    ) -> dict[str, Any]:
        elapsed_seconds = None
        if self._command_started_at is not None:
            elapsed_seconds = time.monotonic() - self._command_started_at

        diagnostic_snapshot: dict[str, Any] | None = None
        try:
            diagnostic_snapshot = await self._robot.get_diagnostic_snapshot()
        except Exception as snapshot_error:
            diagnostic_snapshot = {"snapshot_error": str(snapshot_error)}

        operation_context = None
        try:
            operation_context = self._robot.get_operation_context()
        except Exception as context_error:
            operation_context = {"context_error": str(context_error)}

        fault_context = getattr(err, "fault_context", None)
        if fault_context is None and getattr(err, "__cause__", None) is not None:
            fault_context = getattr(err.__cause__, "fault_context", None)

        context: dict[str, Any] = {
            "command": self._current_command.model_dump(mode="json") if self._current_command else None,
            "queued_operation": self._current_operation.model_dump(mode="json") if self._current_operation else None,
            "elapsed_seconds": elapsed_seconds,
            "operation_context": operation_context,
            "diagnostic_snapshot": diagnostic_snapshot,
            "hardware_fault": {
                "is_hardware_fault": is_hardware_fault,
                "exception_type": type(err).__name__,
                "message": str(err),
                "needs_home_axes": self.needs_home_axes(),
                "fault_context": fault_context,
            },
        }
        return context

    def _build_cancelled_motion_crash_context(self, fault: Exception) -> dict[str, Any]:
        elapsed_seconds = None
        if self._command_started_at is not None:
            elapsed_seconds = time.monotonic() - self._command_started_at

        fault_context = getattr(fault, "fault_context", None)
        if fault_context is None and getattr(fault, "__cause__", None) is not None:
            fault_context = getattr(fault.__cause__, "fault_context", None)

        return {
            "command": self._current_command.model_dump(mode="json") if self._current_command else None,
            "queued_operation": self._current_operation.model_dump(mode="json") if self._current_operation else None,
            "elapsed_seconds": elapsed_seconds,
            "operation_context": None,
            "diagnostic_snapshot": {"snapshot_error": "cancelled before diagnostics completed"},
            "hardware_fault": {
                "is_hardware_fault": True,
                "exception_type": type(fault).__name__,
                "message": str(fault),
                "needs_home_axes": self.needs_home_axes(),
                "fault_context": fault_context,
            },
        }

    def _cancel_motion_crash_capture(
        self, artifact_capture: Awaitable[CrashReportArtifacts] | None
    ) -> None:
        cancel_capture = getattr(artifact_capture, "cancel", None)
        if callable(cancel_capture):
            try:
                cancel_capture()
            except Exception as capture_error:
                logging.warning("Failed to cancel prepared motion crash capture: %s", capture_error)

    def _report_cancelled_motion_crash_handoff(
        self,
        *,
        reason: str,
        fault: Exception,
        stack_trace: str,
        artifact_capture: Awaitable[CrashReportArtifacts] | None,
        report_exception: Exception | None,
    ) -> None:
        self._cancel_motion_crash_capture(artifact_capture)
        context = self._build_cancelled_motion_crash_context(fault)
        self.set_crash_state(reason, context=context)
        self._report_error(
            WorkerError(
                error_type="HardwareFault",
                message=reason,
                context=context,
                stack_trace=stack_trace,
            ),
            exc=report_exception,
        )

    def _report_error(
        self,
        error: WorkerError,
        exc: Optional[Exception] = None,
        *,
        attachments: list[CrashReportAttachment] | None = None,
        add_to_queue: bool = True,
    ) -> None:
        sentry_event_id: Optional[str] = None
        try:
            with sentry_sdk.push_scope() as scope:
                scope.set_tag("error_type", error.error_type)
                scope.set_tag("hardware_fault", str(error.error_type == "HardwareFault").lower())
                context = error.context or {}
                operation_context = context.get("operation_context")
                diagnostic_snapshot = context.get("diagnostic_snapshot")
                hardware_fault = context.get("hardware_fault")
                if self._current_command:
                    scope.set_tag("command", getattr(self._current_command, "command", "unknown"))
                elif context.get("command") and isinstance(context.get("command"), dict):
                    scope.set_tag("command", context["command"].get("command", "unknown"))
                if hardware_fault and isinstance(hardware_fault, dict):
                    scope.set_tag("exception_type", hardware_fault.get("exception_type", "unknown"))
                    needs_home_axes = hardware_fault.get("needs_home_axes")
                    if isinstance(needs_home_axes, list) and len(needs_home_axes) <= 6:
                        scope.set_tag("needs_home_axes", ",".join(str(axis) for axis in needs_home_axes))
                    scope.set_context("hardware_fault", hardware_fault)
                if context:
                    scope.set_context("worker_error", context)
                if operation_context:
                    scope.set_context("robot_operation", operation_context)
                if diagnostic_snapshot:
                    scope.set_context("robot_diagnostics", diagnostic_snapshot)
                if error.stack_trace:
                    scope.set_extra("stack_trace", error.stack_trace)

                for attachment in attachments or []:
                    scope.add_attachment(
                        bytes=attachment.data,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                    )

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

        if add_to_queue is True:
            self._add_error(error)

    async def _report_motion_crash(
        self,
        error: WorkerError,
        exc: Exception | None,
        report_state: dict[str, bool],
        artifact_capture: Awaitable[CrashReportArtifacts] | None = None,
    ) -> None:
        artifacts = CrashReportArtifacts()
        provider = self._crash_artifact_provider

        if artifact_capture is not None:
            try:
                artifacts = await artifact_capture
            except asyncio.CancelledError:
                report_state["reported"] = True
                self._report_error(error, exc=exc, add_to_queue=False)
                raise
            except Exception as artifact_error:
                artifacts.context = {
                    "status": "failed",
                    "error": str(artifact_error),
                    "error_type": type(artifact_error).__name__,
                }
                logging.warning(
                    "Motion crash artifact capture failed: %s",
                    artifact_error,
                    exc_info=True,
                )
        elif provider is not None:
            try:
                artifacts = await provider(error.message)
            except asyncio.CancelledError:
                report_state["reported"] = True
                self._report_error(error, exc=exc, add_to_queue=False)
                raise
            except Exception as artifact_error:
                artifacts.context = {
                    "status": "failed",
                    "error": str(artifact_error),
                    "error_type": type(artifact_error).__name__,
                }
                logging.warning(
                    "Motion crash artifact capture failed: %s",
                    artifact_error,
                    exc_info=True,
                )

        if artifacts.context:
            if error.context is None:
                error.context = {}
            error.context["crash_recorder"] = artifacts.context

        report_state["reported"] = True
        self._report_error(
            error,
            exc=exc,
            attachments=artifacts.attachments,
            add_to_queue=False,
        )

    def _queue_motion_crash_report(
        self,
        error: WorkerError,
        exc: Exception | None = None,
        artifact_capture: Awaitable[CrashReportArtifacts] | None = None,
    ) -> None:
        self._add_error(error)
        report_state = {"reported": False}
        task = self._track_background_task(
            self._report_motion_crash(error, exc, report_state, artifact_capture),
            name="motion-crash-report",
        )

        def _report_if_cancelled_before_reporting(done_task: asyncio.Task[Any]) -> None:
            if done_task.cancelled() is True and report_state["reported"] is False:
                self._cancel_motion_crash_capture(artifact_capture)
                report_state["reported"] = True
                self._report_error(error, exc=exc, add_to_queue=False)

        task.add_done_callback(_report_if_cancelled_before_reporting)

    def _begin_motion_crash_capture(
        self, reason: str
    ) -> Awaitable[CrashReportArtifacts] | None:
        provider = self._crash_artifact_begin_provider
        if provider is None:
            return None

        try:
            return provider(reason)
        except Exception as artifact_error:
            completed = asyncio.get_running_loop().create_future()
            completed.set_result(
                CrashReportArtifacts(
                    context={
                        "status": "failed",
                        "error": str(artifact_error),
                        "error_type": type(artifact_error).__name__,
                    }
                )
            )
            return completed

    def _clear_robot_operation_context(self) -> None:
        try:
            self._robot.clear_operation_context()
        except Exception:
            pass

    async def connect(self) -> None:
        if self._should_enable_barcode() and self._barcode is None:
            port: str = os.getenv("OPENSHELF_BARCODE_PORT", DEFAULT_BARCODE_PORT)  # type: ignore[assignment]
            baudrate_raw: str = os.getenv("OPENSHELF_BARCODE_BAUDRATE", "115200")  # type: ignore[assignment]
            baudrate = int(baudrate_raw)

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

        self._robot.register_light_curtain_listener(self._on_light_curtain_change)
        self._robot.register_cabinet_door_listener(self._on_cabinet_door_change)
        if hasattr(self._robot, "set_operation_context_listener"):
            self._robot.set_operation_context_listener(self._on_operation_context_change)

    async def clear_commands(self) -> None:
        await self._discard_queued_commands("Commands cleared")
        await self._publish_status()

    async def enqueue_command(
        self,
        command: RobotCommands,
        *,
        command_id: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> None:
        async with self._terminalization_lock:
            queued_operation = self._snapshot_command(
                command,
                command_id=command_id,
                visibility=visibility,
                admission_generation=self._interruption_generation,
            )
            self._queued_commands.append(queued_operation)
            self._command_queue.put_nowait(queued_operation)
        try:
            await self._publish_status()
        except Exception as exc:
            logging.warning("Failed to publish queued command status: %s", exc)

    async def subscribe_status(self) -> tuple[asyncio.Queue[str], Optional[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._status_subscribers.append(queue)
        return queue, self._latest_status_payload

    async def unsubscribe_status(self, queue: asyncio.Queue[str]) -> None:
        self._remove_subscriber(self._status_subscribers, queue)

    async def subscribe_events(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._event_subscribers.append(queue)
        return queue

    async def unsubscribe_events(self, queue: asyncio.Queue[str]) -> None:
        self._remove_subscriber(self._event_subscribers, queue)

    def _remove_subscriber(self, subscribers: List[asyncio.Queue[str]], queue: asyncio.Queue[str]) -> None:
        try:
            subscribers.remove(queue)
        except ValueError:
            return

    def clear_crash_state(self) -> None:
        self._is_crashed = False
        self._crash_reason = None
        self._crash_context = None
        if self._safety_motion_blocked is False and self._is_emergency_stopped is False:
            self._robot.release_motion_safety()
        self.clear_needs_home()

    def set_crash_state(self, reason: str, context: Optional[dict[str, Any]] = None) -> None:
        self._is_crashed = True
        self._crash_reason = reason
        self._crash_context = context

    def clear_emergency_stop_state(self) -> None:
        if self._is_estop_active is True:
            reason = ESTOP_UNAVAILABLE_REASON if self._emergency_stop_reason == ESTOP_UNAVAILABLE_REASON else PHYSICAL_ESTOP_REASON
            self._robot.block_motion_for_safety()
            self.set_emergency_stop_state(reason)
            return

        self._is_emergency_stopped = False
        self._emergency_stop_reason = None
        if self._safety_motion_blocked is False and self._is_crashed is False:
            self._robot.release_motion_safety()

    def set_emergency_stop_state(self, reason: str) -> None:
        self._is_emergency_stopped = True
        self._emergency_stop_reason = reason

    def clear_needs_home(self) -> None:
        self._needs_home_axes.clear()
        self._needs_home_unknown = False
        self._needs_home_reason = None

    def clear_needs_home_axes(self, axes: list[str]) -> None:
        for axis in axes:
            self._needs_home_axes.discard(axis)
        if not self._needs_home_axes and not self._needs_home_unknown:
            self._needs_home_reason = None

    def _infer_needs_home_axes(self, reason: str) -> Optional[list[str]]:
        match = _MOTOR_ID_RE.search(reason)
        if not match:
            return None

        try:
            motor_id = int(match.group(1))
        except ValueError:
            return None

        tool = self._robot._tool
        io = self._robot._io
        fridge = getattr(self._robot, "_fridge", None)
        axis_lookup = {
            "x_axis": tool._mx,
            "y_axis": tool._my,
            "z_axis": tool._mg,
            "stage": tool._ms,
            "rotate": tool._mr,
            "door": io._md,
            "fridge_door": fridge._door if fridge is not None else None,
        }
        for axis, motor in axis_lookup.items():
            if motor is not None and getattr(motor, "_motor_id", None) == motor_id:
                return [axis]

        return None

    def set_needs_home(self, reason: str, axes: Optional[list[str]] = None) -> None:
        if axes is None:
            axes = self._infer_needs_home_axes(reason)
        if axes:
            self._needs_home_axes.update(axes)
        else:
            self._needs_home_unknown = True
        self._needs_home_reason = reason

    async def _restore_y_brake_output(self) -> None:
        try:
            await self._robot.restore_y_brake_output()
        except Exception as exc:
            logging.warning("Failed to restore Y-axis brake output: %s", exc)

    async def emergency_stop(self, reason: str) -> None:
        self._robot.block_motion_for_safety()
        self.set_emergency_stop_state(reason)
        # An interrupted move leaves position unknown until the operator homes.
        # Set this before publishing so recovery clients cannot resume bin motion
        # during the stop cleanup window.
        self.set_needs_home(reason)
        await self._publish_status()
        logging.info("Emergency stop triggered: %s", reason)

        async def _cleanup_after_stop() -> None:
            interruption_reason = f"Emergency stop: {reason}"
            interruption_boundary = await self._terminalize_current_command(
                interruption_reason=interruption_reason,
            )
            assert interruption_boundary is not None

            await self._restore_y_brake_output()
            await self._discard_queued_commands(
                interruption_reason,
                interruption_boundary=interruption_boundary,
            )
            await self._publish_status()

        self._track_background_task(_cleanup_after_stop(), name="emergency-stop-cleanup")

    def _is_estop_monitor_enabled(self) -> bool:
        config = getattr(self._robot, "_config", None)
        if config is None:
            return True

        try:
            emergency_stop_config = config.get("robot.emergency_stop", None)
        except Exception:
            emergency_stop_config = None

        if emergency_stop_config is None:
            return True

        if isinstance(emergency_stop_config, dict):
            return bool(emergency_stop_config.get("enabled", True))

        return bool(getattr(emergency_stop_config, "enabled", True))

    async def _start_estop_monitor(self) -> None:
        if self._is_estop_monitor_enabled() is False:
            logging.info("Emergency stop monitor disabled: emergency_stop.enabled is false")
            self._is_estop_active = False
            return

        if self._estop_monitor_task is not None and self._estop_monitor_task.done() is False:
            return

        gpio = getattr(self._robot, "_gpio", None)
        if gpio is None:
            logging.info("Emergency stop monitor disabled: GPIO is not configured")
            return

        try:
            has_pin = getattr(gpio, "has_pin", None)
            if callable(has_pin) is True and has_pin("estop") is False:
                logging.info("Emergency stop monitor disabled: GPIO pin 'estop' is not configured")
                return
            estop_input = gpio.input("estop")
        except Exception as exc:
            logging.warning("Emergency stop input unavailable: %s", exc)
            self._is_estop_active = True
            await self.emergency_stop(ESTOP_UNAVAILABLE_REASON)
            return

        loop = asyncio.get_running_loop()
        self._estop_poll_wakeup = asyncio.Event()

        try:
            self._estop_subscription = estop_input.subscribe(
                lambda _event: loop.call_soon_threadsafe(self._wake_estop_monitor)
            )
        except Exception:
            self._estop_subscription = None
            logging.warning(
                "Emergency stop interrupt subscription failed; continuing with polling",
                exc_info=True,
            )

        self._estop_monitor_task = asyncio.create_task(
            self._monitor_estop(estop_input),
            name="emergency-stop-monitor",
        )
        logging.info(
            "Emergency stop monitor started",
            extra={
                "poll_interval_s": ESTOP_POLL_INTERVAL_SECONDS,
                "subscription_enabled": self._estop_subscription is not None,
                "polling_enabled": True,
            },
        )

    async def _stop_estop_monitor(self) -> None:
        if self._estop_subscription is not None:
            try:
                self._estop_subscription.close()
            except Exception:
                logging.warning("Emergency stop subscription cleanup failed", exc_info=True)
            self._estop_subscription = None

        if self._estop_monitor_task is not None:
            self._estop_monitor_task.cancel()
            try:
                await self._estop_monitor_task
            except asyncio.CancelledError:
                pass
            self._estop_monitor_task = None

        self._estop_poll_wakeup = None
        self._is_estop_active = False

    def _wake_estop_monitor(self) -> None:
        if self._estop_poll_wakeup is not None:
            self._estop_poll_wakeup.set()

    async def _wait_for_estop_poll(self) -> None:
        wakeup = self._estop_poll_wakeup
        if wakeup is None:
            await asyncio.sleep(ESTOP_POLL_INTERVAL_SECONDS)
            return

        try:
            await asyncio.wait_for(wakeup.wait(), timeout=ESTOP_POLL_INTERVAL_SECONDS)
        except TimeoutError:
            pass
        finally:
            wakeup.clear()

    async def _monitor_estop(self, estop_input: Any) -> None:
        previous_active: Optional[bool] = None

        while self._running is True:
            try:
                is_active = estop_input.read() is True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("Emergency stop input read failed: %s", exc)
                self._is_estop_active = True
                if previous_active is not True or self._is_emergency_stopped is False:
                    await self.emergency_stop(ESTOP_UNAVAILABLE_REASON)
                previous_active = True
                await self._wait_for_estop_poll()
                continue

            self._is_estop_active = is_active
            if is_active is True and (previous_active is not True or self._is_emergency_stopped is False):
                await self.emergency_stop(PHYSICAL_ESTOP_REASON)
            elif (
                is_active is False
                and previous_active is True
                and self._is_emergency_stopped is True
                and isinstance(self._current_command, HomeCommand)
                and self._safety_motion_blocked is False
            ):
                self._robot.release_motion_safety()

            if previous_active is True and is_active is False:
                await self._publish_status()

            previous_active = is_active
            await self._wait_for_estop_poll()

    def _should_enable_barcode(self) -> bool:
        disable_raw: str = os.getenv("OPENSHELF_DISABLE_BARCODE", "false")  # type: ignore[assignment]
        return disable_raw.lower() not in {"1", "true", "yes"}

    async def _run_barcode_bridge(self) -> None:
        if not self._barcode:
            return
        try:
            async for code in self._barcode:
                logging.info("Received barcode: %s", code)
                try:
                    await self.publish_event({"kind": "label_scanned", "payload": code})
                except Exception as publish_error:  # pragma: no cover - hardware dependent
                    logging.warning("Barcode event publish error: %s", publish_error)
                    self._report_error(
                        WorkerError(
                            error_type="EventPublishError",
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

    def _snapshot_command(
        self,
        command: RobotCommands,
        *,
        command_id: Optional[str] = None,
        visibility: Optional[str] = None,
        admission_generation: int = 0,
    ) -> QueuedOperation:
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
            admission_generation=admission_generation,
            command_id=command_id or str(uuid.uuid4()),
            visibility=visibility,
            location=location,
            item_data=item_data,
            metadata=metadata,
        )

    async def _get_pending_messages_info(self) -> tuple[int, List[QueuedOperation]]:
        return self._command_queue.qsize(), list(self._queued_commands)

    def _remove_current_operation(self) -> None:
        if self._current_operation is None:
            return

        self._queued_commands = [
            op for op in self._queued_commands
            if op is not self._current_operation
        ]

    async def publish_event(self, payload: str | Dict[str, Any]) -> None:
        if isinstance(payload, str) is True:
            payload_json = payload
        else:
            payload_json = json.dumps(payload)

        for queue in list(self._event_subscribers):
            await queue.put(payload_json)

    async def _publish_command_failure(
        self,
        command: Optional[RobotCommands],
        error: str,
        operation: Optional[QueuedOperation] = None,
        *,
        code: str = "command_interrupted",
    ) -> None:
        """Publish a terminal, correlated failure and persist its lifecycle."""
        if command is None:
            return

        upca: Optional[str] = command.upca if isinstance(command, RetrieveItemCommand) else None
        location = command.location if isinstance(command, RetrieveItemCommand) else None
        if isinstance(command, StoreItemCommand) and isinstance(command.item, dict):
            item_upca = command.item.get("upca")
            if item_upca is not None:
                upca = str(item_upca)
            location = command.item.get("location")

        payload: dict[str, Any] = {
            "command": command.command,
            "message": "failed",
            "command_id": operation.command_id if operation is not None else None,
            "error": error,
            "code": code,
            "request_id": getattr(command, "request_id", None),
            "upca": upca,
            "location": location,
        }
        if operation is not None and operation.visibility == "internal":
            payload["visibility"] = "internal"
        await self.publish_event(payload)
        await self._notify_command_lifecycle("failed", operation, payload)

    async def _notify_command_lifecycle(
        self,
        status: str,
        operation: Optional[QueuedOperation],
        result: Optional[dict[str, Any]],
    ) -> None:
        if operation is None or self._command_lifecycle_callback is None:
            return
        try:
            await self._command_lifecycle_callback(status, operation, result)
        except Exception as exc:
            logging.exception("Failed to persist command lifecycle %s: %s", status, exc)

    async def _publish_completed_command_result(
        self,
        task: asyncio.Task[Any],
        command: Optional[RobotCommands],
        operation: Optional[QueuedOperation] = None,
    ) -> None:
        """Publish a completed command using the normal worker-loop semantics."""
        try:
            result: RobotResponse = task.result()
            logging.info("Command result: %s", type(result).__name__)

            if (
                isinstance(command, (HomeCommand, InitializeCommand))
                and isinstance(result, RobotCommandSuccessful)
            ):
                if self._is_crashed:
                    self.clear_crash_state()
                    logging.info("Crash state cleared after successful %s", command.command)

                if self._is_emergency_stopped:
                    if self._is_estop_active is True:
                        logging.info("Emergency stop state retained while button is active")
                    else:
                        self.clear_emergency_stop_state()
                        logging.info("Emergency stop state cleared after successful %s", command.command)

                if self.needs_home() and isinstance(command, HomeCommand):
                    self.clear_needs_home()
                    logging.info("Needs-home flag cleared after successful homing")

            if isinstance(result, RobotCommandSuccessful):
                payload = result.model_dump(mode="json")
                payload["command_id"] = operation.command_id if operation is not None else None
                payload["request_id"] = getattr(command, "request_id", None)
                if operation is not None and operation.visibility == "internal":
                    payload["visibility"] = "internal"
                await self.publish_event(payload)
                await self._notify_command_lifecycle("succeeded", operation, payload)
            else:
                await self._publish_command_failure(
                    command,
                    result.error,
                    operation,
                    code="execution_failed",
                )
            logging.info("Published command result to event queue")
        except Exception as err:
            logging.warning("Command execution error: %s", err)

            try:
                await self._publish_command_failure(
                    command,
                    str(err),
                    operation,
                    code="execution_error",
                )
            except Exception as publish_error:
                logging.warning("Failed to publish command failure: %s", publish_error)

            is_hardware_fault = isinstance(err, (MotorError, MovementError, ToolError))

            if is_hardware_fault:
                self.set_needs_home(str(err))
                logging.warning("Hardware fault detected - robot in crash state: %s", type(err).__name__)

            prepared_artifacts = (
                self._begin_motion_crash_capture(str(err))
                if is_hardware_fault
                else None
            )

            stack_trace = traceback.format_exc()
            try:
                error_context = await self._build_error_context(
                    err, is_hardware_fault=is_hardware_fault
                )
            except asyncio.CancelledError:
                if is_hardware_fault:
                    self._report_cancelled_motion_crash_handoff(
                        reason=str(err),
                        fault=err,
                        stack_trace=stack_trace,
                        artifact_capture=prepared_artifacts,
                        report_exception=err,
                    )
                raise

            if is_hardware_fault:
                self.set_crash_state(str(err), context=error_context)

            worker_error = WorkerError(
                error_type="HardwareFault" if is_hardware_fault else "CommandExecutionError",
                message=str(err),
                context=error_context,
                stack_trace=stack_trace,
            )
            if is_hardware_fault:
                self._queue_motion_crash_report(
                    worker_error,
                    exc=err,
                    artifact_capture=prepared_artifacts,
                )
            else:
                self._report_error(worker_error, exc=err)

    def _clear_current_command_state(
        self,
        task: Optional[asyncio.Task[Any]],
        command: Optional[RobotCommands],
        operation: Optional[QueuedOperation],
    ) -> None:
        if self._current_task is not task or self._current_command is not command:
            return

        if self._current_operation is operation:
            self._remove_current_operation()
            self._current_operation = None
        self._clear_robot_operation_context()
        self._current_task = None
        self._current_command = None
        self._command_started_at = None

    async def _terminalize_current_command(
        self, *, interruption_reason: Optional[str] = None
    ) -> Optional[int]:
        """Publish one terminal outcome and clear the matching in-flight command state."""
        async with self._terminalization_lock:
            interruption_boundary: Optional[int] = None
            if interruption_reason is not None:
                self._interruption_generation += 1
                self._latest_interruption_reason = interruption_reason
                interruption_boundary = self._interruption_generation

            task = self._current_task
            command = self._current_command
            operation = self._current_operation

            if task is None:
                if interruption_reason is not None:
                    await self._publish_command_failure(command, interruption_reason, operation)
                self._clear_current_command_state(task, command, operation)
                return interruption_boundary

            if task.done() is True:
                if task.cancelled() is True:
                    if interruption_reason is not None:
                        await self._publish_command_failure(command, interruption_reason, operation)
                else:
                    await self._publish_completed_command_result(task, command, operation)
            elif interruption_reason is None:
                return None
            else:
                logging.info("Cancelling current command task during %s", interruption_reason)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logging.warning("Cancelled task failed during cleanup: %s", exc)
                await self._publish_command_failure(command, interruption_reason, operation)

            self._clear_current_command_state(task, command, operation)
            return interruption_boundary

    async def _discard_queued_commands(
        self,
        reason: str,
        *,
        interruption_boundary: Optional[int] = None,
    ) -> None:
        """Fail queued commands admitted before the interruption boundary."""
        async with self._terminalization_lock:
            if interruption_boundary is None:
                self._interruption_generation += 1
                self._latest_interruption_reason = reason
                interruption_boundary = self._interruption_generation

            drained_operations: list[QueuedOperation] = []
            while self._command_queue.empty() is False:
                try:
                    queued_operation = self._command_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                drained_operations.append(queued_operation)

            tracked_operations = [
                queued_operation
                for queued_operation in self._queued_commands
                if queued_operation is not self._current_operation
            ]
            queued_operations = list(drained_operations)
            for queued_operation in tracked_operations:
                if all(queued_operation is not operation for operation in queued_operations):
                    queued_operations.append(queued_operation)

            for queued_operation in queued_operations:
                if queued_operation.admission_generation < interruption_boundary:
                    await self._publish_command_failure(
                        queued_operation.command,
                        reason,
                        queued_operation,
                    )

            for queued_operation in drained_operations:
                if queued_operation.admission_generation >= interruption_boundary:
                    self._command_queue.put_nowait(queued_operation)

            self._queued_commands = [
                queued_operation
                for queued_operation in self._queued_commands
                if (
                    queued_operation is self._current_operation
                    or queued_operation.admission_generation >= interruption_boundary
                )
            ]

    async def _publish_status(self) -> None:
        try:
            robot_status = await self._robot.get_status()
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
        await self._evaluate_safety_guard()
        if robot_status:
            robot_status.cabinet_doors = (
                CabinetDoorState(**self._cabinet_doors)
                if self._cabinet_doors is not None
                else None
            )
            robot_status.io_door_open = self._door_open
            robot_status.safety = SafetyStatus(**self.get_safety_status())
            robot_status.scale = self._latest_scale_status
        status: WorkerStatusWithQueue

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
            crash_context=self._crash_context,
            is_emergency_stopped=self._is_emergency_stopped,
            is_emergency_stop_active=self._is_estop_active,
            emergency_stop_reason=self._emergency_stop_reason,
            needs_home=self.needs_home(),
            needs_home_reason=self._needs_home_reason,
            needs_home_axes=self.needs_home_axes(),
        )

        status_payload = status.model_dump(mode="json")

        if self._current_operation is not None and isinstance(status_payload.get("current_command"), dict):
            status_payload["current_command"]["command_id"] = self._current_operation.command_id
            if self._current_operation.visibility == "internal":
                status_payload["current_command"]["visibility"] = "internal"

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

        for queue in list(self._status_subscribers):
            await queue.put(payload_json)

        await self._maybe_publish_safety_event()

    async def process_commands(self) -> None:
        logger.info("process_commands loop started, running=%s", self._running)
        loop_count = 0

        while self._running:
            try:
                if self._current_task is None:
                    loop_count += 1

                    if loop_count % 30 == 1:
                        logger.debug("process_commands polling, loop=%d", loop_count)

                    try:
                        queued_operation = await asyncio.wait_for(self._command_queue.get(), timeout=1)
                    except asyncio.TimeoutError:
                        safety_changed = await self._evaluate_safety_guard()
                        if safety_changed is True:
                            await self._publish_status()
                        await asyncio.sleep(0.1)
                        continue

                    command = queued_operation.command
                    async with self._terminalization_lock:
                        if queued_operation.admission_generation != self._interruption_generation:
                            if any(operation is queued_operation for operation in self._queued_commands):
                                await self._publish_command_failure(
                                    command,
                                    self._latest_interruption_reason or "Command interrupted",
                                    queued_operation,
                                )
                                self._queued_commands = [
                                    operation
                                    for operation in self._queued_commands
                                    if operation is not queued_operation
                                ]
                            continue
                        self._current_command = command
                        self._current_operation = queued_operation
                    await self._notify_command_lifecycle("running", queued_operation, None)
                    await self._publish_status()

                    async with self._terminalization_lock:
                        if self._current_command is not command or self._current_operation is not queued_operation:
                            continue

                        if isinstance(command, HomeCommand) and (self._is_crashed is True or self._is_emergency_stopped is True):
                            if self._is_estop_active is True:
                                logging.info("Keeping motion safety engaged while emergency stop is active")
                            elif self._safety_motion_blocked is False:
                                self._robot.release_motion_safety()
                                logging.info("Released motion safety for homing in stopped state")

                        logging.info("Starting command execution: %s", command.command)
                        self._command_started_at = time.monotonic()
                        self._current_task = asyncio.create_task(self._robot.handle_command(command))
                else:
                    safety_changed = await self._evaluate_safety_guard()
                    if safety_changed is True:
                        await self._publish_status()

                    if self._current_task.done():
                        logging.info("Command task completed, processing result")
                        await self._terminalize_current_command()
                        await self._publish_status()

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

                if self._current_task is not None and self._current_task.done() is False:
                    logging.warning("Cancelling command task due to unexpected error")
                    self._current_task.cancel()

                    try:
                        await self._current_task
                    except asyncio.CancelledError:
                        pass

                self._clear_robot_operation_context()
                await self._publish_command_failure(
                    self._current_command,
                    str(err),
                    self._current_operation,
                    code="worker_error",
                )
                try:
                    await self._publish_status()
                except Exception as publish_err:
                    logging.warning("Failed to publish error status: %s", publish_err)

                self._remove_current_operation()
                self._current_task = None
                self._current_command = None
                self._current_operation = None
                self._command_started_at = None
                await asyncio.sleep(1)

    async def start(self) -> None:
        logger.info("RobotWorker.start() called, _running=%s", self._running)
        if self._running:
            logger.info("RobotWorker already running, returning early")
            return
        self._running = True
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
        if self._scale_poll_task:
            self._scale_poll_task.cancel()
            try:
                await self._scale_poll_task
            except asyncio.CancelledError:
                pass
            self._scale_poll_task = None
        await self._stop_estop_monitor()
        logger.info("Creating process_commands task")
        self._worker_task = asyncio.create_task(self.process_commands())
        self._scale_poll_task = asyncio.create_task(self._poll_scale_status())
        await self._start_estop_monitor()
        logger.info("Task created: %s", self._worker_task)
        await self._publish_status()
        self._robot.release_motion_safety()
        logger.info("RobotWorker.start() complete")

    async def shutdown(self) -> None:
        logging.info("Shutting down robot worker")
        self._running = False

        if self._scale_poll_task:
            logging.info("Stopping scale poll")
            self._scale_poll_task.cancel()
            try:
                await self._scale_poll_task
            except asyncio.CancelledError:
                pass
            self._scale_poll_task = None

        await self._stop_estop_monitor()

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
            self._worker_task = None

        await self._cancel_background_tasks()
        await self._terminalize_current_command(
            interruption_reason="Robot stopped during shutdown",
        )
        await self._discard_queued_commands("Robot stopped during shutdown")

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
            await self._publish_status()
        except Exception as err:
            self._report_error(
                WorkerError(
                    error_type="ShutdownError",
                    message=str(err),
                    context={"phase": "final_status"},
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

        try:
            sentry_sdk.flush(timeout=2.0)
        except Exception:
            pass

    def get_safety_status(self) -> dict[str, Any]:
        active = bool(self._safety_active_sources)
        return {
            "active": active,
            "motion_blocked": self._safety_motion_blocked,
            "awaiting_ack": self._awaiting_safety_ack,
            "can_acknowledge": self._awaiting_safety_ack is True and active is False,
            "active_sources": list(self._safety_active_sources),
            "sources": self._safety_sources,
            "interruption_id": self._safety_interruption_id,
            "last_changed_at": self._safety_last_changed_at,
            "bypassed": self._safety_guard_bypassed,
            "component_bypasses": dict(self._component_bypasses),
        }

    def is_safety_blocking(self) -> bool:
        if self._safety_guard_bypassed is True:
            return False

        return (
            self._safety_motion_blocked is True
            or self._awaiting_safety_ack is True
            or bool(self._safety_active_sources) is True
        )

    async def _maybe_publish_safety_event(self) -> None:
        safety_status = self.get_safety_status()
        safety_key = json.dumps(safety_status, sort_keys=True)

        if safety_key == self._last_published_safety:
            return

        self._last_published_safety = safety_key
        try:
            await self.publish_event(
                {
                    "kind": "safety_state",
                    "payload": {
                        **safety_status,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                }
            )
            self._logger.debug("Safety event published", extra=safety_status)
        except Exception as err:  # pragma: no cover - observability
            logging.warning("Failed to publish safety event: %s", err)

    def _is_io_door_opening(self) -> bool:
        try:
            context = self._robot.get_operation_context()
        except Exception:
            return False

        if not isinstance(context, dict):
            return False

        phase = context.get("phase")
        return phase in {"io_place.open_door", "io_pick.open_door"}

    def _should_ignore_light_curtain(self) -> bool:
        return self._is_crashed is True or self._is_emergency_stopped is True

    def _build_safety_sources(self) -> tuple[list[str], dict[str, dict[str, Any]]]:
        io_door_opening = self._is_io_door_opening()
        is_bypassed = self._safety_guard_bypassed
        light_curtain_bypassed = is_bypassed or self._component_bypasses["light_curtain"]
        cabinet_doors_bypassed = is_bypassed or self._component_bypasses["cabinet_doors"]
        light_curtain_blocked = (
            self._should_ignore_light_curtain() is False
            and bool(self._curtain_blocked)
            and (bool(self._door_open) or io_door_opening is True)
        )
        light_curtain_active = light_curtain_blocked is True and light_curtain_bypassed is False
        cabinet_doors = dict(self._cabinet_doors or {})
        cabinet_doors_open = cabinet_doors.get("open") is True
        cabinet_doors_active = cabinet_doors_open is True and cabinet_doors_bypassed is False

        sources: dict[str, dict[str, Any]] = {
            "light_curtain": {
                "active": light_curtain_active,
                "blocked": self._curtain_blocked,
                "io_door_open": self._door_open,
                "io_door_opening": io_door_opening,
                "bypassed": light_curtain_bypassed,
            },
            "cabinet_doors": {
                "active": cabinet_doors_active,
                "top_closed": cabinet_doors.get("top_closed"),
                "bottom_closed": cabinet_doors.get("bottom_closed"),
                "closed": cabinet_doors.get("closed"),
                "top_open": cabinet_doors.get("top_open"),
                "bottom_open": cabinet_doors.get("bottom_open"),
                "open": cabinet_doors.get("open"),
                "bypassed": cabinet_doors_bypassed,
            },
        }
        active_sources = [
            name
            for name, source in sources.items()
            if source.get("active") is True
        ]
        return active_sources, sources

    async def _refresh_safety_inputs(self) -> None:
        previous_curtain = self._curtain_blocked
        try:
            self._curtain_blocked = self._robot.get_curtain_state()
        except Exception as exc:  # pragma: no cover - robustness
            logging.debug("Light curtain state refresh failed: %s", exc)

        if self._curtain_blocked != previous_curtain:
            self._logger.debug(
                "Light curtain state updated for safety evaluation",
                extra={"blocked": self._curtain_blocked, "previous": previous_curtain},
            )

        previous_door = self._door_open
        try:
            self._door_open = await self._robot.refresh_door_state()
        except Exception as exc:  # pragma: no cover - robustness
            logging.debug("Door state refresh failed: %s", exc)

        if self._door_open != previous_door:
            self._logger.debug(
                "Door state updated for safety evaluation",
                extra={"door_open": self._door_open, "previous": previous_door},
            )

        try:
            self._cabinet_doors = self._robot.get_cabinet_door_state()
        except Exception as exc:  # pragma: no cover - robustness
            logging.debug("Cabinet door state refresh failed: %s", exc)

    def _apply_safety_state(self, active_sources: list[str], sources: dict[str, dict[str, Any]]) -> bool:
        previous_active_sources = list(self._safety_active_sources)
        previous_sources = dict(self._safety_sources)
        previous_motion_blocked = self._safety_motion_blocked
        is_interrupted = bool(active_sources)
        was_interrupted = bool(previous_active_sources)
        active_sources_changed = active_sources != previous_active_sources
        sources_changed = sources != previous_sources

        previous_active_sources_bypassed = was_interrupted is True and all(
            sources.get(source_name, {}).get("bypassed") is True
            for source_name in previous_active_sources
        )

        if self._safety_guard_bypassed is True:
            self._awaiting_safety_ack = False
        elif is_interrupted is False and previous_active_sources_bypassed is True:
            self._awaiting_safety_ack = False
        elif is_interrupted is True:
            if was_interrupted is False:
                self._safety_interruption_id += 1
            self._awaiting_safety_ack = True
        if active_sources_changed is True:
            self._safety_last_changed_at = datetime.now(timezone.utc).isoformat()

        self._safety_active_sources = active_sources
        self._safety_sources = sources
        self._safety_motion_blocked = is_interrupted is True or self._awaiting_safety_ack is True
        motion_blocked_changed = self._safety_motion_blocked != previous_motion_blocked

        if self._safety_motion_blocked is True and previous_motion_blocked is False:
            self._robot.block_motion_for_safety()
        elif (
            self._safety_motion_blocked is False
            and previous_motion_blocked is True
            and self._is_crashed is False
            and self._is_emergency_stopped is False
        ):
            self._robot.release_motion_safety()

        if active_sources_changed is True or motion_blocked_changed is True:
            self._logger.info(
                "Safety guard change",
                extra={
                    "active_sources": self._safety_active_sources,
                    "motion_blocked": self._safety_motion_blocked,
                    "awaiting_ack": self._awaiting_safety_ack,
                    "interruption_id": self._safety_interruption_id,
                },
            )

        return sources_changed is True or active_sources_changed is True or motion_blocked_changed is True

    def _on_light_curtain_change(self, state: Optional[bool]) -> None:
        async def _handle() -> None:
            if self._should_ignore_light_curtain() is True:
                return

            self._logger.debug(
                "Light curtain sensor change received",
                extra={
                    "blocked": state,
                    "previous": self._curtain_blocked,
                    "door_open_cached": self._door_open,
                },
            )
            self._curtain_blocked = state
            await self._evaluate_safety_guard()
            await self._publish_status()

        self._track_background_task(_handle(), name="light-curtain-change")

    def _on_cabinet_door_change(self, state: dict[str, bool]) -> None:
        async def _handle() -> None:
            self._logger.debug(
                "Cabinet door change received",
                extra={"state": state, "previous": self._cabinet_doors},
            )
            self._cabinet_doors = dict(state)
            await self._evaluate_safety_guard(refresh=False)
            await self._publish_status()

        self._track_background_task(_handle(), name="cabinet-door-change")

    def _on_operation_context_change(self, context: dict[str, Any] | None) -> None:
        async def _handle() -> None:
            self._logger.debug(
                "Operation context change received",
                extra={"context": context},
            )
            await self._evaluate_safety_guard()
            await self._publish_status()

        self._track_background_task(_handle(), name="operation-context-change")

    async def _evaluate_safety_guard(self, *, refresh: bool = True) -> bool:
        async with self._guard_lock:
            if refresh is True:
                await self._refresh_safety_inputs()

            active_sources, sources = self._build_safety_sources()
            did_change = self._apply_safety_state(active_sources, sources)

        await self._maybe_publish_safety_event()
        return did_change

    async def set_safety_guard_bypassed(self, enabled: bool) -> dict[str, Any]:
        async with self._guard_lock:
            if self._safety_guard_bypassed != enabled:
                self._safety_guard_bypassed = enabled
                self._safety_last_changed_at = datetime.now(timezone.utc).isoformat()
                self._robot.set_safety_guard_bypassed(enabled)

            await self._refresh_safety_inputs()
            active_sources, sources = self._build_safety_sources()
            self._apply_safety_state(active_sources, sources)
            safety_status = self.get_safety_status()

        await self._publish_status()
        return safety_status

    async def set_safety_component_bypassed(self, component: str, enabled: bool) -> dict[str, Any]:
        if component not in self._component_bypasses:
            raise ValueError(f"Unknown safety component: {component}")

        async with self._guard_lock:
            if self._component_bypasses[component] != enabled:
                self._component_bypasses[component] = enabled
                self._safety_last_changed_at = datetime.now(timezone.utc).isoformat()
                if component == "light_curtain":
                    self._robot.set_light_curtain_bypassed(enabled)

            await self._refresh_safety_inputs()
            active_sources, sources = self._build_safety_sources()
            self._apply_safety_state(active_sources, sources)
            safety_status = self.get_safety_status()

        await self._publish_status()
        return safety_status

    async def acknowledge_safety_guard(self) -> dict[str, Any]:
        async with self._guard_lock:
            await self._refresh_safety_inputs()
            active_sources, sources = self._build_safety_sources()

            if active_sources:
                self._awaiting_safety_ack = True
                self._apply_safety_state(active_sources, sources)
                accepted = False
            else:
                if self._awaiting_safety_ack is True or self._safety_motion_blocked is True:
                    self._safety_last_changed_at = datetime.now(timezone.utc).isoformat()
                self._safety_active_sources = []
                self._safety_sources = sources
                self._awaiting_safety_ack = False
                if self._safety_motion_blocked is True:
                    self._safety_motion_blocked = False
                    if self._is_crashed is False and self._is_emergency_stopped is False:
                        self._robot.release_motion_safety()
                accepted = True

            safety_status = self.get_safety_status()

        await self._publish_status()
        return {"accepted": accepted, "safety": safety_status}

    async def release_gripper(self) -> None:
        await self._robot.release_gripper()

    async def grip(self) -> None:
        await self._robot.grip()
