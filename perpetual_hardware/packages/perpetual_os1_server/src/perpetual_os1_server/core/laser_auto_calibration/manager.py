# ruff: noqa: F401, F403, F405
from __future__ import annotations

from .models import *
from .scope_runner import ScopeRunnerMixin
from .scan_runner import ScanRunnerMixin
from .angle_runner import AngleRunnerMixin
from .stage_runner import StageRunnerMixin
from .apply_runner import ApplyRunnerMixin
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

    async def _run_session(
        self,
        worker: RobotWorker,
        session_id: str | None,
        requested_scope: LaserAutoCalibrationScope | None = None,
    ) -> None:
        if requested_scope is None:
            requested_scope = LaserAutoCalibrationScope()

        original_params = await self._get_current_calibration_params(worker)

        try:
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
            logger.error("laser_auto_calibration_failed", exc_info=True)
            await self._set_terminal_state("failed", str(exc))
        finally:
            async with self._lock:
                if self._snapshot.session_id == session_id:
                    self._task = None
                    self._active_worker = None
                    self._abort_requested = False


_manager = LaserAutoCalibrationManager()

def get_laser_auto_calibration_manager() -> LaserAutoCalibrationManager:
    return _manager


async def is_laser_auto_calibration_running() -> bool:
    return await _manager.is_active()
