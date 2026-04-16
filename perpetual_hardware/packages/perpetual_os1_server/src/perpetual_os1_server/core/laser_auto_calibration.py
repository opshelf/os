from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from perpetual_os1 import laser_angle_calibration as angle_module
from perpetual_os1 import laser_calibration as laser_module
from perpetual_os1.common import Parameters, resolve_grid_path
from perpetual_os1.laser_scan import ScanBounds
from perpetual_os1.storage import StorageModuleSide
from perpetual_os1.robot_worker import RobotWorker

from perpetual_os1_server.settings import clear_config_cache, get_config_path, get_root_config, save_root_config

logger = logging.getLogger(__name__)

ACTIVE_SESSION_STATES = {"starting", "running", "applying"}
DEFAULT_SCANS_DIR = "/tmp"
AUTO_SCAN_X_MIN_MM = 5.0
RIGHT_AUTO_SCAN_X_MAX_MM = 945.0
ANGLE_DEBUG_DIR_NAME = "laser_auto_calibration_debug"

ANGLE_CAPTURE_RE = re.compile(
    r"phase=capture x=(?P<x_idx>\d+)/(?P<x_count>\d+) z=(?P<z_offset>-?\d+(?:\.\d+)?) rotation=(?P<rotation_idx>\d+)/(?P<rotation_count>\d+)"
)
ANGLE_SAMPLE_RE = re.compile(
    r"sample=(?P<status>ok|invalid) x=(?P<x>-?\d+(?:\.\d+)?) z=(?P<z>-?\d+(?:\.\d+)?) rotation_deg=(?P<rotation>-?\d+(?:\.\d+)?)(?: distance_mm=(?P<distance>-?\d+(?:\.\d+)?))?"
)
ANGLE_SCOUT_SAMPLE_RE = re.compile(
    r"(?P<label>shelf_scout_\d+)=(?P<sample_idx>\d+)/(?P<sample_count>\d+) x=(?P<x>-?\d+(?:\.\d+)?) y=(?P<y>-?\d+(?:\.\d+)?) depth=(?P<depth>none|-?\d+(?:\.\d+)?)"
)
SCAN_SHELF_SCOUT_RE = re.compile(r"phase=shelf-scout scout=(?P<idx>\d+)/(?P<count>\d+) x=(?P<x>-?\d+(?:\.\d+)?)")
SCAN_SHELF_HOLE_RE = re.compile(
    r"phase=shelf-hole-scan shelf=(?P<shelf_idx>\d+)/(?P<shelf_count>\d+) offset=(?P<offset_idx>\d+)/(?P<offset_count>\d+) y=(?P<y>-?\d+(?:\.\d+)?)"
)
SCAN_PROGRESS_SAMPLE_RE = re.compile(
    r"(?P<label>[a-zA-Z0-9_]+)=(?P<sample_idx>\d+)/(?P<sample_count>\d+) x=(?P<x>-?\d+(?:\.\d+)?) y=(?P<y>-?\d+(?:\.\d+)?) depth=(?P<depth>none|-?\d+(?:\.\d+)?)"
)
SHELF_EDGES_RE = re.compile(r"shelf_edges=(?P<count>\d+)")
COARSE_CANDIDATES_RE = re.compile(r"coarse_candidates=(?P<count>\d+)")
SCAN_FINE_RE = re.compile(r"phase=fine-scan candidate=(?P<idx>\d+)/(?P<count>\d+)")
SCAN_FINE_RETRY_RE = re.compile(r"phase=fine-scan-retry candidate=(?P<idx>\d+)/(?P<count>\d+)")
FINE_DETECTED_CELLS_RE = re.compile(r"fine_detected_cells=(?P<count>\d+)")
ANGLE_RESULT_RE = re.compile(r"alpha_deg=(?P<alpha>-?\d+(?:\.\d+)?)")
STAGE_EXTENSION_PROBE_START_MM = 3.0
STAGE_EXTENSION_PROBE_BACKOFF_MM = 3.0
STAGE_EXTENSION_PROBE_Y_OFFSET_MM = 25.0
STAGE_EXTENSION_FAST_APPROACH_MM = 40.0
STAGE_EXTENSION_PROBE_STEP_MM = 0.5
STAGE_EXTENSION_PROBE_TIMEOUT_S = 1.0
STAGE_EXTENSION_NO_PROGRESS_TIMEOUT_S = 0.2


def _should_skip_motion_cleanup_on_cancel() -> bool:
    _, active_exception, _ = sys.exc_info()

    if isinstance(active_exception, asyncio.CancelledError) is True:
        return True

    current_task = asyncio.current_task()

    if current_task is None:
        return False

    return current_task.cancelling() > 0


class LaserAutoCalibrationShelfEdge(BaseModel):
    side: Literal["left_module", "right_module"]
    x_mm: float
    y_mm: float


class LaserAutoCalibrationCandidate(BaseModel):
    side: Literal["left_module", "right_module"]
    shelf_idx: int | None = None
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    row_count: int = 1
    wall_depth_mm: float | None = None
    wall_sample_count: int = 0


class LaserAutoCalibrationCell(BaseModel):
    side: Literal["left_module", "right_module"]
    shelf_idx: int
    cell_idx: int
    tool_x_mm: float
    tool_y_mm: float
    center_x_mm: float
    center_y_mm: float
    width_mm: float
    height_mm: float
    wall_depth_mm: float | None = None


class LaserAutoCalibrationAngleResult(BaseModel):
    side: Literal["left_module", "right_module"]
    alpha_deg: float
    sample_count: int
    capture_y_mm: float
    rms_residual_mm: float
    max_abs_residual_mm: float
    line_angle_deg: float
    previous_rotation_deg: float
    updated_rotation_deg: float


class LaserAutoCalibrationSnapshot(BaseModel):
    state: Literal["idle", "starting", "running", "applying", "completed", "failed", "cancelled"] = "idle"
    session_id: str | None = None
    run_id: str | None = None
    current_side: Literal["left_module", "right_module"] | None = None
    current_stage: str | None = None
    bounds: ScanBounds | None = None
    started_at: str | None = None
    updated_at: str | None = None
    scouted_shelf_count: int = 0
    left_scouted_shelf_count: int = 0
    right_scouted_shelf_count: int = 0
    candidate_count: int = 0
    left_candidate_count: int = 0
    right_candidate_count: int = 0
    detected_cell_count: int = 0
    left_detected_cell_count: int = 0
    right_detected_cell_count: int = 0
    shelf_edges: list[LaserAutoCalibrationShelfEdge] = Field(default_factory=list)
    shelf_candidates: list[LaserAutoCalibrationCandidate] = Field(default_factory=list)
    detected_cells: list[LaserAutoCalibrationCell] = Field(default_factory=list)
    angle_results: list[LaserAutoCalibrationAngleResult] = Field(default_factory=list)
    stage_detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


@dataclass
class _SideState:
    bounds: ScanBounds | None = None
    run_id: str | None = None
    shelf_edges: list[LaserAutoCalibrationShelfEdge] = field(default_factory=list)
    candidates: list[LaserAutoCalibrationCandidate] = field(default_factory=list)
    cells: list[LaserAutoCalibrationCell] = field(default_factory=list)
    angle_result: LaserAutoCalibrationAngleResult | None = None
    stage_extension_mm: float | None = None
    live_shelf_count: int = 0
    live_candidate_count: int = 0
    live_detected_cell_count: int = 0


@dataclass
class _CompletedSideResult:
    side: StorageModuleSide
    bounds: ScanBounds
    analysis: Any
    angle_result: LaserAutoCalibrationAngleResult


def _get_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _side_to_module_name(side: StorageModuleSide) -> Literal["left_module", "right_module"]:
    if side == StorageModuleSide.LEFT:
        return "left_module"

    return "right_module"


def _side_to_short_name(side: StorageModuleSide) -> Literal["left", "right"]:
    if side == StorageModuleSide.LEFT:
        return "left"

    return "right"


def _build_angle_rotation_center_deg(side: StorageModuleSide) -> float:
    return float(angle_module.get_default_rotation_center_deg(_side_to_short_name(side)))


def _side_to_rotation_param_name(side: StorageModuleSide) -> str:
    if side == StorageModuleSide.LEFT:
        return "ROTATION_FRONT"

    return "ROTATION_BACK"


def _side_to_stage_extension_param_name(side: StorageModuleSide) -> str:
    if side == StorageModuleSide.LEFT:
        return "STAGE_EXTENSION_SHELF_FRONT"

    return "STAGE_EXTENSION_SHELF_BACK"


def _build_stage_extension_probe_y_mm(top_shelf_y_mm: float, bounds: ScanBounds) -> float:
	return float(max(bounds.y_min, min(bounds.y_max, top_shelf_y_mm + STAGE_EXTENSION_PROBE_Y_OFFSET_MM)))


def _build_bounds_for_side(scan_config: Any, side: StorageModuleSide) -> ScanBounds:
    prefix = "left" if side == StorageModuleSide.LEFT else "right"

    x_min_mm = float(getattr(scan_config, f"{prefix}_bounds_x_min_mm"))
    x_max_mm = float(getattr(scan_config, f"{prefix}_bounds_x_max_mm"))

    x_min_mm = max(x_min_mm, AUTO_SCAN_X_MIN_MM)

    if side == StorageModuleSide.RIGHT:
        x_max_mm = min(x_max_mm, RIGHT_AUTO_SCAN_X_MAX_MM)

    return ScanBounds(
        x_min=x_min_mm,
        x_max=x_max_mm,
        y_min=float(getattr(scan_config, f"{prefix}_bounds_y_min_mm")),
        y_max=float(getattr(scan_config, f"{prefix}_bounds_y_max_mm")),
    )


def _build_valid_rotation_positions(
    scan_config: Any,
    rotation_center_deg: float,
    rotation_limit_deg: float,
) -> list[float]:
    requested_positions = angle_module.build_axis_positions(
        float(scan_config.angle_calibration_rotation_min_deg),
        float(scan_config.angle_calibration_rotation_max_deg),
        float(scan_config.angle_calibration_rotation_step_deg),
    )
    valid_positions = [
        float(rotation_deg)
        for rotation_deg in requested_positions
        if 0.0 <= rotation_center_deg + float(rotation_deg) <= rotation_limit_deg
    ]

    if len(valid_positions) == 0:
        raise ValueError(
            "rotation sweep has no valid positions "
            f"for center {rotation_center_deg:.3f} deg within 0..{rotation_limit_deg:.3f}"
        )

    if len(valid_positions) != len(requested_positions):
        angle_module.print_status(
            "rotation_sweep_clipped "
            f"requested={len(requested_positions)} "
            f"valid={len(valid_positions)} "
            f"center_deg={rotation_center_deg:.3f} "
            f"rotation_limit_deg={rotation_limit_deg:.3f}"
        )

    return valid_positions


def _build_angle_x_positions(scan_config: Any) -> list[float]:
    x_positions = angle_module.build_axis_positions(
        float(scan_config.angle_calibration_x_max_mm),
        float(scan_config.angle_calibration_x_min_mm),
        float(scan_config.angle_calibration_x_step_mm),
    )

    x_count = int(getattr(scan_config, "angle_calibration_x_count", 5))

    return x_positions[:x_count]


def _build_angle_z_offset_positions(scan_config: Any) -> list[float]:
    z_offset_positions = angle_module.build_axis_positions(
        float(scan_config.angle_calibration_z_offset_min_mm),
        float(scan_config.angle_calibration_z_offset_max_mm),
        float(scan_config.angle_calibration_z_offset_step_mm),
    )
    z_offset_count = int(getattr(scan_config, "angle_calibration_z_offset_count", 1))

    if z_offset_count >= len(z_offset_positions):
        return z_offset_positions

    prioritized_positions = sorted(
        z_offset_positions,
        key=lambda offset_mm: (abs(float(offset_mm)), float(offset_mm)),
    )
    selected_positions = set(prioritized_positions[:z_offset_count])

    return [
        float(offset_mm)
        for offset_mm in z_offset_positions
        if float(offset_mm) in selected_positions
    ]


def _build_estimated_rotation_deg(rotation_center_deg: float, fit_result: Any) -> float:
    return float(rotation_center_deg) + float(fit_result.alpha_deg)


def _build_angle_fit_inputs(
    side: StorageModuleSide,
    scan_config: Any,
    samples: list[angle_module.LaserAngleCalibrationSample],
) -> tuple[list[angle_module.LaserAngleCalibrationSample], float]:
    return samples, float(scan_config.laser_offset_x_mm)


def _build_angle_rotation_positions_for_side(
    side: StorageModuleSide,
    positions: list[float],
) -> list[float]:
    if side != StorageModuleSide.RIGHT:
        return positions

    filtered_positions = [position for position in positions if position >= 0.0]

    if len(filtered_positions) == 0:
        raise ValueError("right-side rotation sweep has no valid positions at or above center")

    return filtered_positions


def _build_angle_calibration_sample(
    *,
    x_mm: float,
    target_z_mm: float,
    rotation_deg: float,
    absolute_rotation_deg: float,
    stage_extension_mm: float,
    depth_mm: float,
) -> angle_module.LaserAngleCalibrationSample:
    return angle_module.LaserAngleCalibrationSample(
        axis_x_mm=float(x_mm),
        axis_z_mm=float(target_z_mm),
        rotation_deg=float(rotation_deg),
        absolute_rotation_deg=float(absolute_rotation_deg),
        stage_extension_mm=float(stage_extension_mm),
        z_extension_mm=float(target_z_mm),
        distance_mm=float(depth_mm),
    )


def _build_candidate_rows(
    side_name: Literal["left_module", "right_module"],
    candidate_rows: list[dict[str, Any]],
    shelf_edges: list[LaserAutoCalibrationShelfEdge],
) -> list[LaserAutoCalibrationCandidate]:
    ordered_edges = sorted(shelf_edges, key=lambda edge: -edge.y_mm)
    candidates: list[LaserAutoCalibrationCandidate] = []

    for row in candidate_rows:
        shelf_idx: int | None = None
        candidate_center_y_mm = (float(row["y_min"]) + float(row["y_max"])) / 2.0

        if len(ordered_edges) > 0:
            shelf_idx = min(
                range(len(ordered_edges)),
                key=lambda idx: abs(ordered_edges[idx].y_mm - candidate_center_y_mm),
            )

        candidates.append(
            LaserAutoCalibrationCandidate(
                side=side_name,
                shelf_idx=shelf_idx,
                x_min=float(row["x_min"]),
                x_max=float(row["x_max"]),
                y_min=float(row["y_min"]),
                y_max=float(row["y_max"]),
                row_count=int(row.get("row_count", 1)),
                wall_depth_mm=(
                    None
                    if row.get("wall_depth_mm") is None
                    else float(row["wall_depth_mm"])
                ),
                wall_sample_count=int(row.get("wall_sample_count", 0)),
            )
        )

    return candidates


def _build_cell_rows(
    side_name: Literal["left_module", "right_module"],
    fine_scan_rows: list[dict[str, Any]],
) -> list[LaserAutoCalibrationCell]:
    cells: list[LaserAutoCalibrationCell] = []

    for fine_row in fine_scan_rows:
        for cell_row in fine_row.get("detected_cells", []):
            cells.append(
                LaserAutoCalibrationCell(
                    side=side_name,
                    shelf_idx=int(cell_row["shelf_idx"]),
                    cell_idx=int(cell_row["cell_idx"]),
                    tool_x_mm=float(cell_row["tool_x_mm"]),
                    tool_y_mm=float(cell_row["tool_y_mm"]),
                    center_x_mm=float(cell_row["center_x_mm"]),
                    center_y_mm=float(cell_row["center_y_mm"]),
                    width_mm=float(cell_row["width_mm"]),
                    height_mm=float(cell_row["height_mm"]),
                    wall_depth_mm=(
                        None
                        if cell_row.get("wall_depth_mm") is None
                        else float(cell_row["wall_depth_mm"])
                    ),
                )
            )

    cells.sort(key=lambda cell: (cell.side, cell.shelf_idx, cell.cell_idx))
    return cells


class LaserAutoCalibrationManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[LaserAutoCalibrationSnapshot]] = set()
        self._task: asyncio.Task[None] | None = None
        self._snapshot = LaserAutoCalibrationSnapshot(updated_at=_get_timestamp())
        self._side_states: dict[str, _SideState] = {}
        self._active_worker: RobotWorker | None = None
        self._abort_requested = False
        self._scans_dir = DEFAULT_SCANS_DIR

    async def get_snapshot(self) -> LaserAutoCalibrationSnapshot:
        async with self._lock:
            return self._snapshot.model_copy(deep=True)

    async def ensure_session_started(self, worker: RobotWorker) -> LaserAutoCalibrationSnapshot:
        async with self._lock:
            if self._task is not None and self._task.done() is False:
                return self._snapshot.model_copy(deep=True)

            started_at = _get_timestamp()
            self._side_states = {}
            self._active_worker = worker
            self._abort_requested = False
            self._snapshot = LaserAutoCalibrationSnapshot(
                state="starting",
                session_id=uuid4().hex,
                current_stage="session-start",
                started_at=started_at,
                updated_at=started_at,
            )
            self._task = asyncio.create_task(self._run_session(worker, self._snapshot.session_id))
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

    async def _run_session(self, worker: RobotWorker, session_id: str | None) -> None:
        original_params = await self._get_current_calibration_params(worker)

        try:
            await self._run_all_sides(worker)
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

    async def _run_all_sides(self, worker: RobotWorker) -> None:
        completed_results: list[_CompletedSideResult] = []
        scan_config = get_root_config().robot.grid.scan
        left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
        right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)

        right_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.RIGHT, right_bounds)
        await self._apply_angle_calibration_in_memory(worker, StorageModuleSide.RIGHT, right_angle_result)

        left_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.LEFT, left_bounds)
        await self._apply_angle_calibration_in_memory(worker, StorageModuleSide.LEFT, left_angle_result)

        right_probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
            worker,
            StorageModuleSide.RIGHT,
            right_bounds,
        )
        right_stage_extension_mm = await self._run_stage_extension_calibration_for_side(
            worker,
            StorageModuleSide.RIGHT,
            right_bounds,
            probe_y_mm=right_probe_y_mm,
        )
        await self._apply_stage_extension_calibration_in_memory(
            worker,
            StorageModuleSide.RIGHT,
            right_stage_extension_mm,
        )

        left_probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
            worker,
            StorageModuleSide.LEFT,
            left_bounds,
            top_shelf_y_mm=float(left_angle_result.capture_y_mm),
        )
        left_stage_extension_mm = await self._run_stage_extension_calibration_for_side(
            worker,
            StorageModuleSide.LEFT,
            left_bounds,
            probe_y_mm=left_probe_y_mm,
        )
        await self._apply_stage_extension_calibration_in_memory(
            worker,
            StorageModuleSide.LEFT,
            left_stage_extension_mm,
        )

        completed_results.append(await self._run_side(worker, StorageModuleSide.RIGHT, right_angle_result))
        completed_results.append(await self._run_side(worker, StorageModuleSide.LEFT, left_angle_result))

        await self._persist_completed_results(worker, completed_results)

    async def _home_after_abort(self, worker: RobotWorker) -> None:
        if worker._robot._tool._mx is not None:
            await worker._robot._tool._mx.move_to_position(angle_module.RIGHT_MODULE_ROTATION_SAFE_X_MM)

        await worker._robot._tool.home()

        try:
            await worker._robot._io.home_door()
        except Exception:
            logger.warning("laser_auto_calibration_abort_door_home_failed", exc_info=True)

        worker.clear_needs_home()
        await worker.get_status_snapshot(force_refresh=True)

    async def _run_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        angle_result: LaserAutoCalibrationAngleResult,
    ) -> _CompletedSideResult:
        side_name = _side_to_module_name(side)
        config = get_root_config()
        scan_config = config.robot.grid.scan
        bounds = _build_bounds_for_side(scan_config, side)
        self._side_states.setdefault(side_name, _SideState(bounds=bounds))

        await self._apply_angle_calibration_in_memory(worker, side, angle_result)

        config = get_root_config()
        scan_config = config.robot.grid.scan
        settle_ms = int(scan_config.settle_ms)
        samples_per_point = int(scan_config.samples_per_point)
        coarse_settle_ms = int(laser_module.get_coarse_settle_ms(settle_ms))
        coarse_samples_per_point = int(laser_module.get_coarse_samples_per_point(samples_per_point))
        args = SimpleNamespace(
            module=_side_to_short_name(side),
            x=(bounds.x_min, bounds.x_max),
            y=(bounds.y_min, bounds.y_max),
            y_offset=0.0,
            settle_ms=None,
            samples_per_point=None,
            known_shelf_edge_y=None,
            npy=None,
            scans_dir=self._scans_dir,
            config=str(get_config_path() or Path("data/config.json")),
            apply=True,
            shelf_scout_only=False,
            rescan_cell=None,
        )
        run_parameters = laser_module.build_run_parameters(
            args=args,
            side=side,
            bounds=bounds,
            step_mm=float(scan_config.step_mm),
            settle_ms=settle_ms,
            samples_per_point=samples_per_point,
            coarse_settle_ms=coarse_settle_ms,
            coarse_samples_per_point=coarse_samples_per_point,
            scan_config=scan_config,
        )
        recorder = laser_module.create_staged_scan_recorder(
            scans_dir=self._scans_dir,
            run_parameters=run_parameters,
        )

        await self._refresh_scan_side_state(side_name, recorder, bounds)
        logger.info(
            "laser_auto_calibration_scan_start side=%s run_dir=%s",
            side_name,
            str(recorder.run_dir),
        )

        async with self._patch_status_callbacks(
            scan_callback=self._build_scan_status_callback(side_name, recorder, bounds),
            angle_callback=None,
        ):
            analysis = await laser_module.run_staged_scan(
                robot=worker._robot,
                side=side,
                bounds=bounds,
                fine_step_mm=float(scan_config.step_mm),
                settle_ms=settle_ms,
                samples_per_point=samples_per_point,
                coarse_settle_ms=coarse_settle_ms,
                coarse_samples_per_point=coarse_samples_per_point,
                scan_config=scan_config,
                scans_dir=self._scans_dir,
                run_parameters=run_parameters,
                shelf_scout_only=False,
                known_shelf_edge_y=None,
                recorder=recorder,
                should_abort=self._should_abort_work,
            )

        await self._refresh_scan_side_state(side_name, recorder, bounds)
        await self._refresh_scan_side_state(side_name, recorder, bounds)
        await self._update_snapshot(
            current_side=side_name,
            current_stage="side-complete",
            state="running",
            run_id=analysis.run_id,
            bounds=bounds,
            stage_detail={"side": side_name, "phase": "completed", "run_id": analysis.run_id},
        )

        return _CompletedSideResult(
            side=side,
            bounds=bounds,
            analysis=analysis,
            angle_result=angle_result,
        )

    async def _run_angle_calibration_for_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
    ) -> LaserAutoCalibrationAngleResult:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        tool = worker._robot._tool

        if tool._laser_enabled is False or tool._laser is None:
            raise ValueError("Laser is not enabled")

        if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
            raise ValueError("x, y, z, stage, and rotation motors are required")

        side_name = _side_to_short_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        x_positions = _build_angle_x_positions(scan_config)
        z_offset_positions = _build_angle_z_offset_positions(scan_config)
        settle_ms = int(scan_config.settle_ms)
        samples_per_point = int(scan_config.samples_per_point)
        stage_extension_mm = float(
            scan_config.scan_stage_extension_mm
            if scan_config.scan_stage_extension_mm is not None
            else angle_module.LASER_SCAN_STAGE_EXTENSION_MM
        )
        scan_z_offset_mm = float(
            scan_config.scan_z_offset_mm
            if scan_config.scan_z_offset_mm is not None
            else angle_module.LASER_SCAN_Z_OFFSET_MM
        )
        rotation_param = _side_to_rotation_param_name(side)

        previous_rotation_deg = float(await tool.get_param(rotation_param))
        rotation_center_deg = _build_angle_rotation_center_deg(side)
        rotation_limit_deg = float(tool._mr._params.lim)
        rotation_positions = _build_valid_rotation_positions(
            scan_config,
            rotation_center_deg,
            rotation_limit_deg,
        )
        rotation_positions = _build_angle_rotation_positions_for_side(side, rotation_positions)
        z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
        z_move_rpm = await tool.get_param("Z_MOVE_RPM")
        z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

        if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
            raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

        base_z_mm = float(z_extension_place) + scan_z_offset_mm
        angle_args = SimpleNamespace(
            y_mm=None,
            y_range=(bounds.y_min, bounds.y_max),
            scout_x_mm=None,
        )
        samples: list[Any] = []

        async with self._patch_status_callbacks(
            scan_callback=None,
            angle_callback=self._build_angle_status_callback(_side_to_module_name(side)),
        ):
            try:
                self._raise_if_abort_requested()
                await tool.retract()
                capture_y_mm = await angle_module.resolve_capture_y_mm(
                    tool,
                    args=angle_args,
                    scan_config=scan_config,
                    side_name=side_name,
                    side_number=side_number,
                    x_positions=x_positions,
                    rotation_center_deg=rotation_center_deg,
                    stage_extension_mm=stage_extension_mm,
                    base_z_mm=base_z_mm,
                    z_move_rpm=float(z_move_rpm),
                    z_move_accel=float(z_move_accel),
                    settle_ms=settle_ms,
                    samples_per_point=samples_per_point,
                    should_abort=self._should_abort_work,
                )
                self._raise_if_abort_requested()
                current_z_mm = None
                current_absolute_rotation_deg: float | None = rotation_center_deg

                for z_offset_mm in z_offset_positions:
                    self._raise_if_abort_requested()
                    target_z_mm = base_z_mm + float(z_offset_mm)

                    for x_idx, x_mm in enumerate(x_positions, start=1):
                        self._raise_if_abort_requested()
                        for rotation_idx, rotation_deg in enumerate(rotation_positions, start=1):
                            self._raise_if_abort_requested()
                            absolute_rotation_deg = rotation_center_deg + float(rotation_deg)
                            angle_module.print_status(
                                f"phase=capture x={x_idx}/{len(x_positions)} z={z_offset_mm:.2f} "
                                f"rotation={rotation_idx}/{len(rotation_positions)} "
                                f"absolute_rotation_deg={absolute_rotation_deg:.3f}"
                            )
                            await angle_module.prepare_capture_pose(
                                tool,
                                side_name=side_name,
                                side_number=side_number,
                                x_mm=float(x_mm),
                                y_mm=capture_y_mm,
                                absolute_rotation_deg=absolute_rotation_deg,
                                stage_extension_mm=stage_extension_mm,
                                z_mm=target_z_mm,
                                z_move_rpm=float(z_move_rpm),
                                z_move_accel=float(z_move_accel),
                                should_rotate=current_absolute_rotation_deg != absolute_rotation_deg,
                                should_use_safe_right_rotation_path=False,
                                should_move_z=current_z_mm != target_z_mm,
                            )
                            self._raise_if_abort_requested()
                            current_z_mm = target_z_mm
                            current_absolute_rotation_deg = absolute_rotation_deg
                            depth_mm = await angle_module.sample_laser_depth(
                                tool,
                                settle_ms=settle_ms,
                                samples_per_point=samples_per_point,
                            )
                            self._raise_if_abort_requested()

                            if depth_mm is None:
                                angle_module.print_status(
                                    f"sample=invalid x={x_mm:.3f} z={target_z_mm:.3f} rotation_deg={rotation_deg:.3f}"
                                )
                                continue

                            samples.append(
                                _build_angle_calibration_sample(
                                    x_mm=float(x_mm),
                                    target_z_mm=target_z_mm,
                                    rotation_deg=float(rotation_deg),
                                    absolute_rotation_deg=float(absolute_rotation_deg),
                                    stage_extension_mm=stage_extension_mm,
                                    depth_mm=float(depth_mm),
                                )
                            )
                            angle_module.print_status(
                                f"sample=ok x={x_mm:.3f} z={target_z_mm:.3f} rotation_deg={rotation_deg:.3f} distance_mm={depth_mm:.3f}"
                            )
                self._raise_if_abort_requested()
                fit_samples, laser_offset_x_mm = _build_angle_fit_inputs(side, scan_config, samples)
                fit_result = angle_module.search_best_alpha(
                    fit_samples,
                    alpha_min_deg=float(scan_config.angle_calibration_alpha_min_deg),
                    alpha_max_deg=float(scan_config.angle_calibration_alpha_max_deg),
                    coarse_step_deg=float(scan_config.angle_calibration_coarse_step_deg),
                    fine_window_deg=float(scan_config.angle_calibration_fine_window_deg),
                    fine_step_deg=float(scan_config.angle_calibration_fine_step_deg),
                    laser_offset_x_mm=laser_offset_x_mm,
                    laser_offset_z_mm=0.0,
                    beam_zero_deg=float(scan_config.angle_calibration_beam_zero_deg),
                )
                angle_module.print_fit_result(fit_result)
            finally:
                if _should_skip_motion_cleanup_on_cancel() is False:
                    await tool.retract()

        updated_rotation_deg = _build_estimated_rotation_deg(rotation_center_deg, fit_result)

        if updated_rotation_deg < 0.0 or updated_rotation_deg > rotation_limit_deg:
            raise ValueError(
                "estimated rotation is outside motor range "
                f"({updated_rotation_deg:.3f} deg is outside 0..{rotation_limit_deg:.3f})"
            )

        return LaserAutoCalibrationAngleResult(
            side=_side_to_module_name(side),
            alpha_deg=float(fit_result.alpha_deg),
            sample_count=int(fit_result.sample_count),
            capture_y_mm=float(capture_y_mm),
            rms_residual_mm=float(fit_result.line.rms_residual_mm),
            max_abs_residual_mm=float(fit_result.line.max_abs_residual_mm),
            line_angle_deg=float(fit_result.line.line_angle_deg),
            previous_rotation_deg=previous_rotation_deg,
            updated_rotation_deg=updated_rotation_deg,
        )

    async def _apply_angle_calibration_in_memory(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        result: LaserAutoCalibrationAngleResult,
    ) -> None:
        rotation_param = _side_to_rotation_param_name(side)
        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict[rotation_param] = result.updated_rotation_deg
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)
        side_name = _side_to_module_name(side)
        self._side_states.setdefault(side_name, _SideState()).angle_result = result

        await self._update_snapshot(
            current_side=side_name,
            current_stage="angle-calibration",
            state="running",
            stage_detail={
                "side": side_name,
                "phase": "angle-applied",
                "alpha_deg": result.alpha_deg,
                "updated_rotation_deg": result.updated_rotation_deg,
            },
        )

    async def _run_stage_extension_calibration_for_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
        *,
        probe_y_mm: float,
    ) -> float:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        tool = worker._robot._tool

        if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
            raise ValueError("x, y, z, stage, and rotation motors are required")

        if probe_y_mm < bounds.y_min or probe_y_mm > bounds.y_max:
            raise ValueError(
                f"stage extension probe y {probe_y_mm:.3f} is outside bounds {bounds.y_min:.3f}..{bounds.y_max:.3f}"
            )

        side_name = _side_to_short_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        side_module_name = _side_to_module_name(side)
        rotation_param = _side_to_rotation_param_name(side)
        absolute_rotation_deg = float(await tool.get_param(rotation_param))
        z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
        z_move_rpm = await tool.get_param("Z_MOVE_RPM")
        z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

        if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
            raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

        probe_x_mm = float((bounds.x_min + bounds.x_max) / 2.0)
        scan_z_offset_mm = float(
            scan_config.scan_z_offset_mm
            if scan_config.scan_z_offset_mm is not None
            else angle_module.LASER_SCAN_Z_OFFSET_MM
        )
        base_z_mm = float(z_extension_place) + scan_z_offset_mm
        stage_limit_mm = float(tool._ms._params.lim)
        probe_rpm = min(int(tool._ms._params.rpm), 100)

        await self._update_snapshot(
            current_side=side_module_name,
            current_stage="stage-extension-calibration",
            state="running",
            bounds=bounds,
            stage_detail={
                "side": side_module_name,
                "phase": "stage-extension-calibration",
                "subphase": "move",
                "probe_x_mm": probe_x_mm,
                "probe_y_mm": float(probe_y_mm),
                "absolute_rotation_deg": absolute_rotation_deg,
                "probe_rpm": probe_rpm,
                "stage_target_mm": stage_limit_mm,
            },
        )

        try:
            self._raise_if_abort_requested()
            await tool.retract()
            await angle_module.prepare_capture_pose(
                tool,
                side_name=side_name,
                side_number=side_number,
                x_mm=probe_x_mm,
                y_mm=float(probe_y_mm),
                absolute_rotation_deg=absolute_rotation_deg,
                stage_extension_mm=STAGE_EXTENSION_PROBE_START_MM,
                z_mm=base_z_mm,
                z_move_rpm=float(z_move_rpm),
                z_move_accel=float(z_move_accel),
                should_rotate=True,
                should_use_safe_right_rotation_path=side == StorageModuleSide.RIGHT,
                should_move_z=True,
            )
            self._raise_if_abort_requested()
            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "probe",
                    "probe_x_mm": probe_x_mm,
                    "probe_y_mm": float(probe_y_mm),
                    "absolute_rotation_deg": absolute_rotation_deg,
                    "probe_rpm": probe_rpm,
                    "stage_target_mm": stage_limit_mm,
                },
            )
            did_stall, stage_position_mm, status, alarm_code = await angle_module.probe_stage_until_stall(
                tool,
                target_stage_mm=stage_limit_mm,
                retract_stage_mm=STAGE_EXTENSION_PROBE_START_MM,
                settle_ms=int(scan_config.settle_ms),
                timeout_s=STAGE_EXTENSION_PROBE_TIMEOUT_S,
                probe_rpm=probe_rpm,
                no_progress_s=STAGE_EXTENSION_NO_PROGRESS_TIMEOUT_S,
                probe_step_mm=STAGE_EXTENSION_PROBE_STEP_MM,
                fast_approach_mm=STAGE_EXTENSION_FAST_APPROACH_MM,
                fast_approach_rpm=int(tool._ms._params.rpm),
                auto_retract=False,
                should_abort=self._should_abort_work,
            )
            self._raise_if_abort_requested()

            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "probe-result",
                    "probe_x_mm": probe_x_mm,
                    "probe_y_mm": float(probe_y_mm),
                    "absolute_rotation_deg": absolute_rotation_deg,
                    "stall_status": status,
                    "alarm_code": alarm_code,
                    "stall_stage_mm": float(stage_position_mm),
                    "stage_target_mm": stage_limit_mm,
                },
            )

            if did_stall is False:
                raise ValueError(
                    f"stage extension probe reached limit without stall at {stage_position_mm:.3f} mm"
                )

            calibrated_stage_extension_mm = max(0.0, stage_position_mm - STAGE_EXTENSION_PROBE_BACKOFF_MM)
            self._raise_if_abort_requested()
            await tool._ms.move_to_position(calibrated_stage_extension_mm, rpm=probe_rpm)
            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "backoff",
                    "probe_x_mm": probe_x_mm,
                    "probe_y_mm": float(probe_y_mm),
                    "absolute_rotation_deg": absolute_rotation_deg,
                    "stall_status": status,
                    "alarm_code": alarm_code,
                    "stall_stage_mm": float(stage_position_mm),
                    "calibrated_stage_extension_mm": calibrated_stage_extension_mm,
                },
            )
            return calibrated_stage_extension_mm
        finally:
            if _should_skip_motion_cleanup_on_cancel() is False:
                await tool.retract()

    async def _resolve_stage_extension_probe_y_mm(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
        *,
        top_shelf_y_mm: float | None = None,
    ) -> float:
        side_name = _side_to_short_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        side_module_name = _side_to_module_name(side)

        if top_shelf_y_mm is None:
            config = get_root_config()
            scan_config = config.robot.grid.scan
            tool = worker._robot._tool

            if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
                raise ValueError("x, y, z, stage, and rotation motors are required")

            z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
            z_move_rpm = await tool.get_param("Z_MOVE_RPM")
            z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

            if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
                raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

            x_positions = _build_angle_x_positions(scan_config)
            scan_stage_extension_mm = float(
                scan_config.scan_stage_extension_mm
                if scan_config.scan_stage_extension_mm is not None
                else angle_module.LASER_SCAN_STAGE_EXTENSION_MM
            )
            scan_z_offset_mm = float(
                scan_config.scan_z_offset_mm
                if scan_config.scan_z_offset_mm is not None
                else angle_module.LASER_SCAN_Z_OFFSET_MM
            )
            absolute_rotation_deg = float(await tool.get_param(_side_to_rotation_param_name(side)))
            base_z_mm = float(z_extension_place) + scan_z_offset_mm
            angle_args = SimpleNamespace(
                y_mm=None,
                y_range=(bounds.y_min, bounds.y_max),
                scout_x_mm=None,
            )

            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "shelf-scout",
                },
            )

            await tool.retract()
            top_shelf_y_mm = await angle_module.resolve_capture_y_mm(
                tool,
                args=angle_args,
                scan_config=scan_config,
                side_name=side_name,
                side_number=side_number,
                x_positions=x_positions,
                rotation_center_deg=absolute_rotation_deg,
                stage_extension_mm=scan_stage_extension_mm,
                base_z_mm=base_z_mm,
                z_move_rpm=float(z_move_rpm),
                z_move_accel=float(z_move_accel),
                settle_ms=int(scan_config.settle_ms),
                samples_per_point=int(scan_config.samples_per_point),
                should_abort=self._should_abort_work,
            )

        probe_y_mm = _build_stage_extension_probe_y_mm(float(top_shelf_y_mm), bounds)
        await self._update_snapshot(
            current_side=side_module_name,
            current_stage="stage-extension-calibration",
            state="running",
            bounds=bounds,
            stage_detail={
                "side": side_module_name,
                "phase": "stage-extension-calibration",
                "subphase": "probe-y",
                "top_shelf_y_mm": float(top_shelf_y_mm),
                "probe_y_mm": probe_y_mm,
            },
        )
        return probe_y_mm

    async def _apply_stage_extension_calibration_in_memory(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        stage_extension_mm: float,
    ) -> None:
        stage_extension_param = _side_to_stage_extension_param_name(side)
        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict[stage_extension_param] = stage_extension_mm
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)
        side_name = _side_to_module_name(side)
        self._side_states.setdefault(side_name, _SideState()).stage_extension_mm = stage_extension_mm

        await self._update_snapshot(
            current_side=side_name,
            current_stage="stage-extension-calibration",
            state="running",
            stage_detail={
                "side": side_name,
                "phase": "stage-extension-applied",
                "stage_extension_param": stage_extension_param,
                "calibrated_stage_extension_mm": float(stage_extension_mm),
            },
        )


    async def _persist_completed_results(
        self,
        worker: RobotWorker,
        completed_results: list[_CompletedSideResult],
    ) -> None:
        await self._update_snapshot(
            state="applying",
            current_stage="apply",
            stage_detail={
                "phase": "apply",
                "side_count": len(completed_results),
            },
        )

        config = get_root_config()
        next_config = config.model_copy(deep=True)
        params_dict = next_config.robot.tool.parameters.model_dump()

        for completed_result in completed_results:
            rotation_param = _side_to_rotation_param_name(completed_result.side)
            stage_extension_param = _side_to_stage_extension_param_name(completed_result.side)
            side_state = self._side_states.get(_side_to_module_name(completed_result.side))

            if side_state is None or side_state.stage_extension_mm is None:
                raise ValueError(f"Missing stage extension calibration result for {_side_to_module_name(completed_result.side)}")

            params_dict[rotation_param] = completed_result.angle_result.updated_rotation_deg
            params_dict[stage_extension_param] = float(side_state.stage_extension_mm)

        next_config.robot.tool.parameters = Parameters.model_validate(params_dict)

        scan_config = next_config.robot.grid.scan
        grid_path = Path(resolve_grid_path(next_config))
        source_grid = laser_module.load_grid(grid_path)
        left_module = list(source_grid.left_module)
        right_module = list(source_grid.right_module)

        for completed_result in completed_results:
            if len(completed_result.analysis.shelves) == 0:
                raise ValueError("No tray openings detected from scan")

            existing_shelves = left_module

            if completed_result.side == StorageModuleSide.RIGHT:
                existing_shelves = right_module

            scanned_shelves = laser_module.build_shelves_from_scan(
                side=completed_result.side,
                analysis=completed_result.analysis,
                existing_shelves=existing_shelves,
            )
            laser_module.apply_xy_reference_offsets(
                scanned_shelves=scanned_shelves,
                scan_config=scan_config,
                side=completed_result.side,
            )
            laser_module.apply_wall_depth_z_offsets(
                analysis=completed_result.analysis,
                scanned_shelves=scanned_shelves,
                metadata_path=str(completed_result.analysis.metadata_path),
                scan_config=scan_config,
                side=completed_result.side,
            )

            if completed_result.side == StorageModuleSide.LEFT:
                left_module = scanned_shelves
            else:
                right_module = scanned_shelves

        storage_grid = laser_module.StorageGrid(
            left_module=left_module,
            right_module=right_module,
            filename=source_grid.filename if source_grid.filename != "" else str(grid_path),
        )

        try:
            storage_grid.save()
            save_root_config(next_config)
            clear_config_cache()
            worker._robot.reload_parameters()
        except Exception:
            try:
                source_grid.save()
            except Exception:
                logger.error("laser_auto_calibration_grid_rollback_failed", exc_info=True)

            try:
                save_root_config(config)
                clear_config_cache()
                worker._robot.reload_parameters()
            except Exception:
                logger.error("laser_auto_calibration_config_rollback_failed", exc_info=True)

            raise

        worker._robot._grid = laser_module.load_grid(grid_path)

    async def _get_current_calibration_params(self, worker: RobotWorker) -> dict[str, float]:
        front = await worker._robot._tool.get_param("ROTATION_FRONT")
        back = await worker._robot._tool.get_param("ROTATION_BACK")
        front_stage_extension = await worker._robot._tool.get_param("STAGE_EXTENSION_SHELF_FRONT")
        back_stage_extension = await worker._robot._tool.get_param("STAGE_EXTENSION_SHELF_BACK")

        return {
            "ROTATION_FRONT": float(front),
            "ROTATION_BACK": float(back),
            "STAGE_EXTENSION_SHELF_FRONT": float(front_stage_extension),
            "STAGE_EXTENSION_SHELF_BACK": float(back_stage_extension),
        }


    def _restore_in_memory_calibration_params(self, worker: RobotWorker, params: dict[str, float]) -> None:
        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict["ROTATION_FRONT"] = float(params["ROTATION_FRONT"])
        params_dict["ROTATION_BACK"] = float(params["ROTATION_BACK"])
        params_dict["STAGE_EXTENSION_SHELF_FRONT"] = float(params["STAGE_EXTENSION_SHELF_FRONT"])
        params_dict["STAGE_EXTENSION_SHELF_BACK"] = float(params["STAGE_EXTENSION_SHELF_BACK"])
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)

    async def _refresh_scan_side_state(
        self,
        side_name: Literal["left_module", "right_module"],
        recorder: Any,
        bounds: ScanBounds,
    ) -> None:
        side_state = self._side_states.setdefault(side_name, _SideState())
        side_state.bounds = bounds
        side_state.run_id = recorder.run_id

        if recorder.metadata_path.exists() is True:
            metadata = self._read_json(recorder.metadata_path)
            side_state.run_id = str(metadata.get("run_id", recorder.run_id))
            side_state.shelf_edges = [
                LaserAutoCalibrationShelfEdge(
                    side=side_name,
                    x_mm=float(row["x_mm"]),
                    y_mm=float(row["y_mm"]),
                )
                for row in metadata.get("shelf_edges", [])
            ]
            side_state.candidates = _build_candidate_rows(
                side_name,
                list(metadata.get("candidates", [])),
                side_state.shelf_edges,
            )
            side_state.cells = _build_cell_rows(side_name, list(metadata.get("fine_scans", [])))
            side_state.live_shelf_count = max(side_state.live_shelf_count, len(side_state.shelf_edges))
            side_state.live_candidate_count = max(side_state.live_candidate_count, len(side_state.candidates))
            side_state.live_detected_cell_count = max(side_state.live_detected_cell_count, len(side_state.cells))

        progress_payload: dict[str, Any] = {}

        if recorder.progress_path.exists() is True:
            progress_payload = self._read_json(recorder.progress_path)

        await self._sync_snapshot(
            run_id=side_state.run_id,
            current_side=side_name,
            bounds=bounds,
            stage_detail={
                **self._snapshot.stage_detail,
                "side": side_name,
                "scan_progress": progress_payload.get("progress", {}),
                "scan_phase": progress_payload.get("phase"),
            },
        )

    def _read_json(self, path: Path) -> dict[str, Any]:
        return __import__("json").loads(path.read_text(encoding="utf-8"))

    def _should_preserve_cancelling_snapshot(self) -> bool:
        return self._abort_requested is True or self._snapshot.current_stage == "cancelling"

    def _should_abort_work(self) -> bool:
        return self._abort_requested is True or self._snapshot.current_stage == "cancelling"

    def _raise_if_abort_requested(self) -> None:
        if self._should_abort_work() is True:
            raise asyncio.CancelledError()

    def _build_scan_status_callback(self, side_name: str, recorder: Any, bounds: ScanBounds):
        async def did_receive_status(message: str) -> None:
            if self._should_preserve_cancelling_snapshot() is True:
                return

            current_stage = self._snapshot.current_stage
            stage_detail = dict(self._snapshot.stage_detail)
            stage_detail["side"] = side_name
            stage_detail["log"] = message
            side_state = self._side_states.setdefault(side_name, _SideState())

            shelf_match = SCAN_SHELF_SCOUT_RE.search(message)
            shelf_hole_match = SCAN_SHELF_HOLE_RE.search(message)
            progress_sample_match = SCAN_PROGRESS_SAMPLE_RE.search(message)
            shelf_edges_match = SHELF_EDGES_RE.search(message)
            coarse_candidates_match = COARSE_CANDIDATES_RE.search(message)
            fine_match = SCAN_FINE_RE.search(message)
            fine_retry_match = SCAN_FINE_RETRY_RE.search(message)
            fine_detected_cells_match = FINE_DETECTED_CELLS_RE.search(message)

            if message.startswith("phase=coarse-scan") is True or shelf_match is not None:
                current_stage = "shelf-scout"

            if shelf_match is not None:
                stage_detail.update(
                    {
                        "phase": "shelf-scout",
                        "scout_idx": int(shelf_match.group("idx")),
                        "scout_count": int(shelf_match.group("count")),
                        "x_position": float(shelf_match.group("x")),
                    }
                )

            if progress_sample_match is not None:
                laser_distance_mm: float | None = None

                if progress_sample_match.group("depth") != "none":
                    laser_distance_mm = float(progress_sample_match.group("depth"))

                stage_detail.update(
                    {
                        "laser_sample_label": progress_sample_match.group("label"),
                        "laser_sample_idx": int(progress_sample_match.group("sample_idx")),
                        "laser_sample_count": int(progress_sample_match.group("sample_count")),
                        "laser_sample_x_mm": float(progress_sample_match.group("x")),
                        "laser_sample_y_mm": float(progress_sample_match.group("y")),
                        "laser_distance_mm": laser_distance_mm,
                    }
                )

            if message.startswith("phase=coarse-detect") is True or shelf_hole_match is not None:
                current_stage = "coarse-detect"

            if shelf_hole_match is not None:
                side_state.live_shelf_count = max(
                    side_state.live_shelf_count,
                    int(shelf_hole_match.group("shelf_count")),
                )
                stage_detail.update(
                    {
                        "phase": "coarse-detect",
                        "shelf_idx": int(shelf_hole_match.group("shelf_idx")),
                        "shelf_count": int(shelf_hole_match.group("shelf_count")),
                        "offset_idx": int(shelf_hole_match.group("offset_idx")),
                        "offset_count": int(shelf_hole_match.group("offset_count")),
                        "y_position": float(shelf_hole_match.group("y")),
                    }
                )

            if shelf_edges_match is not None:
                side_state.live_shelf_count = max(
                    side_state.live_shelf_count,
                    int(shelf_edges_match.group("count")),
                )

            if coarse_candidates_match is not None:
                side_state.live_candidate_count = max(
                    side_state.live_candidate_count,
                    int(coarse_candidates_match.group("count")),
                )

            if fine_detected_cells_match is not None:
                side_state.live_detected_cell_count = max(
                    side_state.live_detected_cell_count,
                    int(fine_detected_cells_match.group("count")),
                )

            if fine_match is not None or fine_retry_match is not None:
                current_stage = "fine-scan"
                active_match = fine_match or fine_retry_match
                side_state.live_candidate_count = max(
                    side_state.live_candidate_count,
                    int(active_match.group("count")),
                )
                stage_detail.update(
                    {
                        "phase": "fine-scan",
                        "candidate_idx": int(active_match.group("idx")),
                        "candidate_count": int(active_match.group("count")),
                        "is_retry": fine_retry_match is not None,
                    }
                )

            if fine_detected_cells_match is not None:
                current_stage = "fine-scan"

            if message.startswith("run_dir=") is True:
                stage_detail["run_dir"] = message.split("=", 1)[1]

            await self._refresh_scan_side_state(side_name, recorder, bounds)
            await self._update_snapshot(
                current_side=side_name,
                current_stage=current_stage,
                state="running",
                bounds=bounds,
                stage_detail=stage_detail,
            )

        return did_receive_status

    def _build_angle_status_callback(self, side_name: str):
        async def did_receive_status(message: str) -> None:
            if self._should_preserve_cancelling_snapshot() is True:
                return

            stage_detail = dict(self._snapshot.stage_detail)
            stage_detail["side"] = side_name
            stage_detail["log"] = message
            stage_detail["phase"] = "angle-calibration"

            capture_match = ANGLE_CAPTURE_RE.search(message)
            sample_match = ANGLE_SAMPLE_RE.search(message)
            scout_sample_match = ANGLE_SCOUT_SAMPLE_RE.search(message)
            alpha_match = ANGLE_RESULT_RE.search(message)

            if message.startswith("phase=shelf-scout") is True:
                stage_detail["subphase"] = "shelf-scout"

            if message.startswith("shelf_edges=") is True:
                stage_detail["subphase"] = "shelf-scout"

            if message.startswith("angle_capture_y_mm=") is True:
                stage_detail["capture_y_mm"] = float(message.split("=", 1)[1])

            if scout_sample_match is not None:
                laser_distance_mm: float | None = None

                if scout_sample_match.group("depth") != "none":
                    laser_distance_mm = float(scout_sample_match.group("depth"))

                stage_detail.update(
                    {
                        "subphase": "shelf-scout",
                        "laser_sample_label": scout_sample_match.group("label"),
                        "laser_sample_idx": int(scout_sample_match.group("sample_idx")),
                        "laser_sample_count": int(scout_sample_match.group("sample_count")),
                        "laser_sample_x_mm": float(scout_sample_match.group("x")),
                        "laser_sample_y_mm": float(scout_sample_match.group("y")),
                        "laser_distance_mm": laser_distance_mm,
                    }
                )

            if capture_match is not None:
                stage_detail.update(
                    {
                        "subphase": "capture",
                        "x_idx": int(capture_match.group("x_idx")),
                        "x_count": int(capture_match.group("x_count")),
                        "z_offset_mm": float(capture_match.group("z_offset")),
                        "rotation_idx": int(capture_match.group("rotation_idx")),
                        "rotation_count": int(capture_match.group("rotation_count")),
                    }
                )

            if sample_match is not None:
                stage_detail.update(
                    {
                        "subphase": "capture",
                        "sample_status": sample_match.group("status"),
                        "sample_x_mm": float(sample_match.group("x")),
                        "sample_z_mm": float(sample_match.group("z")),
                        "sample_rotation_deg": float(sample_match.group("rotation")),
                        "laser_distance_mm": (
                            None
                            if sample_match.group("distance") is None
                            else float(sample_match.group("distance"))
                        ),
                    }
                )

            if alpha_match is not None:
                stage_detail.update(
                    {
                        "subphase": "fit",
                        "alpha_deg": float(alpha_match.group("alpha")),
                    }
                )

            await self._update_snapshot(
                current_side=side_name,
                current_stage="angle-calibration",
                state="running",
                stage_detail=stage_detail,
            )

        return did_receive_status

    @asynccontextmanager
    async def _patch_status_callbacks(self, scan_callback=None, angle_callback=None):
        original_scan_print = laser_module.print_status
        original_angle_print = angle_module.print_status
        pending_task: asyncio.Task[None] | None = None

        def didSchedule(callback: Any, message: str) -> None:
            nonlocal pending_task

            previous_task = pending_task

            async def didRunCallback() -> None:
                if previous_task is not None:
                    await previous_task

                await callback(message)

            pending_task = asyncio.create_task(didRunCallback())

        def did_print_scan(message: str) -> None:
            original_scan_print(message)

            if scan_callback is not None:
                didSchedule(scan_callback, message)

        def did_print_angle(message: str) -> None:
            original_angle_print(message)

            if angle_callback is not None:
                didSchedule(angle_callback, message)

        laser_module.print_status = did_print_scan
        angle_module.print_status = did_print_angle

        try:
            yield
        finally:
            laser_module.print_status = original_scan_print
            angle_module.print_status = original_angle_print

            if pending_task is not None:
                current_task = asyncio.current_task()

                if self._should_abort_work() is True or (current_task is not None and current_task.cancelling() > 0):
                    pending_task.cancel()

                    with suppress(asyncio.CancelledError):
                        await pending_task
                else:
                    await pending_task

    async def _set_terminal_state(self, state: Literal["completed", "failed", "cancelled"], error: str | None) -> None:
        await self._update_snapshot(
            state=state,
            current_stage=state,
            error=error,
            stage_detail={**self._snapshot.stage_detail, "phase": state},
        )

    async def _update_snapshot(self, **updates: Any) -> None:
        snapshot = await self._sync_snapshot(**updates)
        await self._publish(snapshot)

    async def _sync_snapshot(self, **updates: Any) -> LaserAutoCalibrationSnapshot:
        async with self._lock:
            for key, value in updates.items():
                setattr(self._snapshot, key, value)

            self._snapshot.updated_at = _get_timestamp()

            shelf_edges: list[LaserAutoCalibrationShelfEdge] = []
            candidates: list[LaserAutoCalibrationCandidate] = []
            cells: list[LaserAutoCalibrationCell] = []
            angle_results: list[LaserAutoCalibrationAngleResult] = []

            for side_state in self._side_states.values():
                shelf_edges.extend(side_state.shelf_edges)
                candidates.extend(side_state.candidates)
                cells.extend(side_state.cells)

                if side_state.angle_result is not None:
                    angle_results.append(side_state.angle_result)

            self._snapshot.shelf_edges = sorted(
                shelf_edges,
                key=lambda edge: (edge.side, -edge.y_mm, edge.x_mm),
            )
            self._snapshot.shelf_candidates = sorted(
                candidates,
                key=lambda candidate: (candidate.side, candidate.shelf_idx if candidate.shelf_idx is not None else 999, candidate.y_min, candidate.x_min),
            )
            self._snapshot.detected_cells = sorted(
                cells,
                key=lambda cell: (cell.side, cell.shelf_idx, cell.cell_idx),
            )
            self._snapshot.angle_results = sorted(angle_results, key=lambda result: result.side)
            left_side_state = self._side_states.get("left_module", _SideState())
            right_side_state = self._side_states.get("right_module", _SideState())
            self._snapshot.left_scouted_shelf_count = max(
                len([edge for edge in self._snapshot.shelf_edges if edge.side == "left_module"]),
                left_side_state.live_shelf_count,
            )
            self._snapshot.right_scouted_shelf_count = max(
                len([edge for edge in self._snapshot.shelf_edges if edge.side == "right_module"]),
                right_side_state.live_shelf_count,
            )
            self._snapshot.left_candidate_count = max(
                len([candidate for candidate in self._snapshot.shelf_candidates if candidate.side == "left_module"]),
                left_side_state.live_candidate_count,
            )
            self._snapshot.right_candidate_count = max(
                len([candidate for candidate in self._snapshot.shelf_candidates if candidate.side == "right_module"]),
                right_side_state.live_candidate_count,
            )
            self._snapshot.left_detected_cell_count = max(
                len([cell for cell in self._snapshot.detected_cells if cell.side == "left_module"]),
                left_side_state.live_detected_cell_count,
            )
            self._snapshot.right_detected_cell_count = max(
                len([cell for cell in self._snapshot.detected_cells if cell.side == "right_module"]),
                right_side_state.live_detected_cell_count,
            )
            self._snapshot.scouted_shelf_count = max(
                len(self._snapshot.shelf_edges),
                self._snapshot.left_scouted_shelf_count + self._snapshot.right_scouted_shelf_count,
            )
            self._snapshot.candidate_count = max(
                len(self._snapshot.shelf_candidates),
                self._snapshot.left_candidate_count + self._snapshot.right_candidate_count,
            )
            self._snapshot.detected_cell_count = max(
                len(self._snapshot.detected_cells),
                self._snapshot.left_detected_cell_count + self._snapshot.right_detected_cell_count,
            )
            snapshot = self._snapshot.model_copy(deep=True)

        return snapshot

    async def _publish(self, snapshot: LaserAutoCalibrationSnapshot) -> None:
        async with self._lock:
            subscribers = list(self._subscribers)

        for queue in subscribers:
            if queue.full() is True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

            try:
                queue.put_nowait(snapshot.model_copy(deep=True))
            except asyncio.QueueFull:
                continue


_manager = LaserAutoCalibrationManager()


def get_laser_auto_calibration_manager() -> LaserAutoCalibrationManager:
    return _manager


async def is_laser_auto_calibration_running() -> bool:
    return await _manager.is_active()
