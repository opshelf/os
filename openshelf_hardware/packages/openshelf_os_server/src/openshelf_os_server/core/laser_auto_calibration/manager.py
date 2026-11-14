from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4
from pathlib import Path

from .models import *
from .models import LaserAutoCalibrationError, LaserAutoCalibrationErrorRef
from .scope_runner import ScopeRunnerMixin
from .scan_runner import ScanRunnerMixin
from .angle_runner import AngleRunnerMixin
from .stage_runner import StageRunnerMixin
from .apply_runner import ApplyRunnerMixin
from .gripper_runner import GripperRunnerMixin
from openshelf_os_server.core.logging_config import get_correlation_id
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.errors import failure_detail
from .snapshot_runner import SnapshotRunnerMixin
from openshelf_os.robot.worker import fault_needs_home_axes


class LaserAutoCalibrationManager(
    ScopeRunnerMixin,
    ScanRunnerMixin,
    AngleRunnerMixin,
    StageRunnerMixin,
    ApplyRunnerMixin,
    GripperRunnerMixin,
    SnapshotRunnerMixin,
):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[LaserAutoCalibrationSnapshot]] = set()
        self._task: asyncio.Task[None] | None = None
        self._snapshot = LaserAutoCalibrationSnapshot(updated_at=_get_timestamp())
        self._side_states: dict[str, _SideState] = {}
        self._reference_offset_entries: list[LaserAutoCalibrationReferenceOffset] = []
        self._active_worker: RobotWorker | None = None
        self._abort_requested = False
        # Set once abort()'s direct stop has returned; the abort home waits for it so that stop
        # can never land on the home.
        self._abort_stop_done: asyncio.Event | None = None
        self._scans_dir = _build_default_scans_dir()
        self._motion_diagnostic_session_id: str | None = None
        self._motion_diagnostic_correlation_id: str | None = None
        # Scan run directory, set only while a scan is in progress.
        self._run_dir: Path | None = None

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
        self._validate_requested_scope_config(requested_scope, worker)

        async with self._lock:
            if self._task is not None and self._task.done() is False:
                if self._snapshot.requested_scope.model_dump() != requested_scope.model_dump():
                    raise RuntimeError("Laser auto calibration is already running with a different scope")

                return self._snapshot.model_copy(deep=True)

            started_at = _get_timestamp()
            self._side_states = {}
            self._reference_offset_entries = []
            self._active_worker = worker
            self._abort_requested = False
            self._abort_stop_done = None
            self._run_dir = None
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
        async with self._lock:
            if self._task is None or self._task.done() is True:
                raise RuntimeError("No laser auto calibration session is running")

            if self._snapshot.current_stage == "cancelling":
                return self._snapshot.model_copy(deep=True)

            worker = self._active_worker
            stop_done = asyncio.Event()
            self._abort_stop_done = stop_done
            self._abort_requested = True
            self._task.cancel()
            self._snapshot.current_stage = "cancelling"
            self._snapshot.updated_at = _get_timestamp()
            snapshot = self._snapshot.model_copy(deep=True)

        # Stop directly as well: a step that is slow to unwind from the cancel would keep moving.
        try:
            if worker is not None:
                await worker._robot.stop_motion()
        finally:
            stop_done.set()

        await self._publish(snapshot)
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

        original_params: Any = None
        session_correlation_id = get_correlation_id()
        self._motion_diagnostic_session_id = session_id
        self._motion_diagnostic_correlation_id = session_correlation_id

        self._set_motion_diagnostic_context(
            worker,
            run_id=session_id,
            correlation_id=session_correlation_id,
        )

        try:
            original_params = await self._get_current_calibration_params(worker)
            # The abort's stop and home run inside the guard, so no manual move can start
            # between the cancelled step and the home.
            async with manual_motion_guard("laser_auto_calibration", worker=worker):
                try:
                    async with self._hold_y_enabled(worker, requested_scope):
                        await self._run_requested_scope(worker, requested_scope)
                except asyncio.CancelledError:
                    await self._finish_abort(worker, original_params)
                    return
                except Exception:
                    if self._abort_requested is False:
                        raise
                    # A step interrupted by the abort can fail with a hardware error instead of
                    # the cancellation; it still has to end in the abort's home.
                    logger.warning("laser_auto_calibration_abort_interrupted_step", exc_info=True)
                    await self._finish_abort(worker, original_params)
                    return
            await self._set_terminal_state("completed", None)
        except asyncio.CancelledError:
            # Cancelled before the scope ran or after it finished: none of its motion is in flight.
            await _absorb_cancellation()
            if original_params is not None:
                self._restore_in_memory_calibration_params(worker, original_params)
            await self._set_terminal_state("cancelled", None)
        except Exception as exc:
            try:
                await self._fail_session(worker, exc, original_params)
            except asyncio.CancelledError:
                # An abort landed during the failure bookkeeping. The real failure came first and
                # no motion is in flight on this path, so end failed rather than stuck cancelling.
                await _absorb_cancellation()
                await self._set_terminal_state("failed", failure_detail(exc, "laser_calibration_failed")[1])
        finally:
            self._set_motion_diagnostic_context(worker)

            self._motion_diagnostic_session_id = None
            self._motion_diagnostic_correlation_id = None

            async with self._lock:
                if self._snapshot.session_id == session_id:
                    self._task = None
                    self._active_worker = None
                    self._abort_requested = False
                    self._abort_stop_done = None


    async def _fail_session(self, worker: RobotWorker, exc: Exception, original_params: Any) -> None:
        if original_params is not None:
            self._restore_in_memory_calibration_params(worker, original_params)
        error_detail = await self._build_error_detail(worker, exc)
        logger.error(
            "laser_auto_calibration_failed",
            extra={"error_detail": error_detail.model_dump(mode="json")},
            exc_info=True,
        )
        await self._persist_failure_metadata(error_detail)
        # error is operator copy; the exception stays in the log line and run metadata.
        await self._set_terminal_state(
            "failed",
            failure_detail(exc, "laser_calibration_failed")[1],
            error_detail=LaserAutoCalibrationErrorRef(
                phase=error_detail.phase, correlation_id=error_detail.correlation_id
            ),
        )

    async def _finish_abort(self, worker: RobotWorker, original_params: Any) -> None:
        await _absorb_cancellation()
        stop_done = self._abort_stop_done
        if stop_done is not None:
            await stop_done.wait()
        self._restore_in_memory_calibration_params(worker, original_params)

        try:
            await worker._robot.stop_motion()
            await self._home_after_abort(worker)
        except Exception as exc:
            logger.error("laser_auto_calibration_abort_home_failed", exc_info=True)
            worker.set_needs_home(
                "Laser auto calibration abort homing failed",
                axes=fault_needs_home_axes(worker._robot, exc),
            )
            await worker.get_status_snapshot(force_refresh=True)
            await self._set_terminal_state(
                "failed",
                "The laser calibration stopped, but the robot could not be homed. Reset the robot before continuing.",
            )
            return

        await self._set_terminal_state("cancelled", None)

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
        run_dir = self._run_dir

        if run_dir is None:
            return

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

async def _absorb_cancellation() -> None:
    """Consume this task's pending cancellations so the abort's home is not cancelled with it."""
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling() > 0:
        # A cancel requested but not yet delivered fires at the next await; take it here.
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        task.uncancel()
