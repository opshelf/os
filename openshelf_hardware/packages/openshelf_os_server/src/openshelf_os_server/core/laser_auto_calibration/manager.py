# ruff: noqa: F401, F403, F405
from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4
from pathlib import Path

from .models import *
from .models import LaserAutoCalibrationError
from .scope_runner import ScopeRunnerMixin
from .scan_runner import ScanRunnerMixin
from .angle_runner import AngleRunnerMixin
from .stage_runner import StageRunnerMixin
from .apply_runner import ApplyRunnerMixin
from openshelf_os_server.core.logging_config import get_correlation_id
from openshelf_os_server.core.manual_motion import manual_motion_guard
from .snapshot_runner import SnapshotRunnerMixin


class LaserAutoCalibrationManager(
    ScopeRunnerMixin,
    ScanRunnerMixin,
    AngleRunnerMixin,
    StageRunnerMixin,
    ApplyRunnerMixin,
    SnapshotRunnerMixin,
):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[LaserAutoCalibrationSnapshot]] = set()
        self._task: asyncio.Task[None] | None = None
        self._snapshot = LaserAutoCalibrationSnapshot(updated_at=_get_timestamp())
        self._side_states: dict[str, _SideState] = {}
        self._active_worker: RobotWorker | None = None
        self._abort_requested = False
        self._scans_dir = _build_default_scans_dir()
        self._motion_diagnostic_session_id: str | None = None
        self._motion_diagnostic_correlation_id: str | None = None

    async def get_snapshot(self) -> LaserAutoCalibrationSnapshot:
        async with self._lock:
            return self._snapshot.model_copy(deep=True)

    async def ensure_session_started(
        self,
        worker: RobotWorker,
        requested_scope: LaserAutoCalibrationScope | None = None,
    ) -> LaserAutoCalibrationSnapshot:
        if requested_scope is None:
            requested_scope = LaserAutoCalibrationScope()
        self._validate_requested_scope_config(requested_scope)

        async with self._lock:
            if self._task is not None and self._task.done() is False:
                if self._snapshot.requested_scope.model_dump() != requested_scope.model_dump():
                    raise RuntimeError("Laser auto calibration is already running with a different scope")

                return self._snapshot.model_copy(deep=True)

            started_at = _get_timestamp()
            self._side_states = {}
            self._active_worker = worker
            self._abort_requested = False
            self._snapshot = LaserAutoCalibrationSnapshot(
                state="starting",
                session_id=uuid4().hex,
                requested_scope=requested_scope.model_copy(deep=True),
                current_stage="session-start",
                started_at=started_at,
                updated_at=started_at,
            )
            self._task = asyncio.create_task(
                self._run_session(
                    worker,
                    self._snapshot.session_id,
                    requested_scope.model_copy(deep=True),
                )
            )
            snapshot = self._snapshot.model_copy(deep=True)

        await self._publish(snapshot)
        return snapshot

    async def abort(self) -> LaserAutoCalibrationSnapshot:
        worker: RobotWorker | None = None

        async with self._lock:
            if self._task is None or self._task.done() is True:
                raise RuntimeError("No laser auto calibration session is running")

            if self._snapshot.current_stage == "cancelling":
                return self._snapshot.model_copy(deep=True)

            worker = self._active_worker
            self._abort_requested = True
            self._task.cancel()
            self._snapshot.current_stage = "cancelling"
            self._snapshot.updated_at = _get_timestamp()
            snapshot = self._snapshot.model_copy(deep=True)

        await self._publish(snapshot)

        if worker is not None:
            await worker._robot.stop()

        return snapshot

    async def subscribe(self) -> tuple[asyncio.Queue[LaserAutoCalibrationSnapshot], LaserAutoCalibrationSnapshot]:
        queue: asyncio.Queue[LaserAutoCalibrationSnapshot] = asyncio.Queue(maxsize=20)

        async with self._lock:
            self._subscribers.add(queue)
            snapshot = self._snapshot.model_copy(deep=True)

        return queue, snapshot

    async def unsubscribe(self, queue: asyncio.Queue[LaserAutoCalibrationSnapshot]) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    async def is_active(self) -> bool:
        async with self._lock:
            return self._task is not None and self._task.done() is False

    def _set_motion_diagnostic_context(
        self,
        worker: RobotWorker,
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> None:
        robot = getattr(worker, "_robot", None)
        tool = getattr(robot, "_tool", None)
        set_context: Any = getattr(tool, "set_motion_diagnostic_context", None)

        if callable(set_context) is True:
            set_context(run_id=run_id, correlation_id=correlation_id)

    async def _run_session(
        self,
        worker: RobotWorker,
        session_id: str | None,
        requested_scope: LaserAutoCalibrationScope | None = None,
    ) -> None:
        if requested_scope is None:
            requested_scope = LaserAutoCalibrationScope()

        original_params = await self._get_current_calibration_params(worker)
        session_correlation_id = get_correlation_id()
        self._motion_diagnostic_session_id = session_id
        self._motion_diagnostic_correlation_id = session_correlation_id

        self._set_motion_diagnostic_context(
            worker,
            run_id=session_id,
            correlation_id=session_correlation_id,
        )

        try:
            async with manual_motion_guard(
                "laser_auto_calibration", worker=worker
            ), self._hold_y_enabled(worker, requested_scope):
                await self._run_requested_scope(worker, requested_scope)
            await self._set_terminal_state("completed", None)
        except asyncio.CancelledError:
            current_task = asyncio.current_task()

            if current_task is not None:
                while current_task.cancelling() > 0:
                    current_task.uncancel()

            self._restore_in_memory_calibration_params(worker, original_params)

            try:
                await self._home_after_abort(worker)
            except Exception as exc:
                logger.error("laser_auto_calibration_abort_home_failed", exc_info=True)
                worker.set_needs_home("Laser auto calibration abort homing failed")
                await worker.get_status_snapshot(force_refresh=True)
                await self._set_terminal_state("failed", f"Abort homing failed: {exc}")
                return

            await self._set_terminal_state("cancelled", None)
            return
        except Exception as exc:
            if self._abort_requested:
                raise asyncio.CancelledError() from exc
            self._restore_in_memory_calibration_params(worker, original_params)
            error_detail = await self._build_error_detail(worker, exc)
            logger.error(
                "laser_auto_calibration_failed",
                extra={"error_detail": error_detail.model_dump(mode="json")},
                exc_info=True,
            )
            await self._persist_failure_metadata(error_detail)
            await self._set_terminal_state("failed", str(exc), error_detail=error_detail)
        finally:
            self._set_motion_diagnostic_context(worker)

            self._motion_diagnostic_session_id = None
            self._motion_diagnostic_correlation_id = None

            async with self._lock:
                if self._snapshot.session_id == session_id:
                    self._task = None
                    self._active_worker = None
                    self._abort_requested = False


    def _hold_y_enabled(
        self,
        worker: RobotWorker,
        requested_scope: LaserAutoCalibrationScope,
    ) -> contextlib.AbstractAsyncContextManager[None]:
        tool = getattr(getattr(worker, "_robot", None), "_tool", None)
        y_motor = getattr(tool, "_my", None)

        if requested_scope.hold_y_enabled is False or y_motor is None:
            return contextlib.nullcontext()

        return y_motor.hold_enabled()

    async def _build_error_detail(
        self,
        worker: RobotWorker,
        exc: Exception,
    ) -> LaserAutoCalibrationError:
        diagnostic_snapshot: dict[str, Any] | None = None
        try:
            raw_snapshot: Any = await worker.get_status_snapshot(force_refresh=True)
            if hasattr(raw_snapshot, "model_dump"):
                diagnostic_snapshot = raw_snapshot.model_dump(mode="json")
            elif isinstance(raw_snapshot, dict):
                diagnostic_snapshot = raw_snapshot
        except Exception as snapshot_exc:
            diagnostic_snapshot = {"error": f"failed to capture diagnostic snapshot: {snapshot_exc}"}

        fault_context = getattr(exc, "fault_context", None)
        if isinstance(fault_context, dict) is False:
            cause = getattr(exc, "__cause__", None)
            fault_context = getattr(cause, "fault_context", None)

        if isinstance(fault_context, dict) is False:
            fault_context = None

        return LaserAutoCalibrationError(
            error_type=type(exc).__name__,
            error=str(exc),
            phase=self._snapshot.current_stage,
            run_id=self._snapshot.run_id,
            correlation_id=get_correlation_id(),
            fault_context=fault_context,
            diagnostic_snapshot=diagnostic_snapshot,
        )

    async def _persist_failure_metadata(self, error_detail: LaserAutoCalibrationError) -> None:
        run_dir_value = self._snapshot.stage_detail.get("run_dir")

        if run_dir_value is None:
            return

        run_dir = Path(str(run_dir_value))

        metadata_path = run_dir / "metadata.json"
        if metadata_path.exists() is False:
            return

        try:
            data = __import__("json").loads(metadata_path.read_text(encoding="utf-8"))
            data["status"] = "failed"
            data["failed_at"] = _get_timestamp()
            data["error"] = error_detail.error
            data["error_type"] = error_detail.error_type
            data["error_detail"] = error_detail.model_dump(mode="json")
            metadata_path.write_text(
                __import__("json").dumps(data, indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        except Exception:
            logger.warning(
                "laser_auto_calibration_failure_metadata_persist_failed",
                extra={"metadata_path": str(metadata_path)},
                exc_info=True,
            )

_manager = LaserAutoCalibrationManager()

def get_laser_auto_calibration_manager() -> LaserAutoCalibrationManager:
    return _manager


async def is_laser_auto_calibration_running() -> bool:
    return await _manager.is_active()
