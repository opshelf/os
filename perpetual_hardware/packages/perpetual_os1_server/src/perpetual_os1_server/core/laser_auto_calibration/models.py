# ruff: noqa: F401, F403, F405
from __future__ import annotations

import asyncio
import argparse
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import math
from pathlib import Path
import re
import sys
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
DEFAULT_SCANS_DIR_NAME = "calibration"
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
STAGE_EXTENSION_PROBE_START_MM = 0.0
STAGE_EXTENSION_PROBE_Y_OFFSET_MM = 25.0
STAGE_EXTENSION_PROBE_STEP_MM = 0.5
STAGE_EXTENSION_PROBE_TIMEOUT_S = 60.0
STAGE_EXTENSION_NO_PROGRESS_TIMEOUT_S = 0.2
STAGE_EXTENSION_STALL_BACKOFF_MM = 5.0
IO_BOX_MIN_CONFIGURED_LASER_OFFSET_X_MM = 1.0


def _resolve_io_box_laser_offset_x_mm(scan_config: Any) -> float:
    laser_offset_x_mm = laser_module.require_scan_config_parameter(scan_config, "laser_offset_x_mm")

    if abs(laser_offset_x_mm) < IO_BOX_MIN_CONFIGURED_LASER_OFFSET_X_MM:
        raise ValueError("robot.grid.scan.laser_offset_x_mm must be configured before IO box calibration")

    return laser_offset_x_mm


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


class LaserAutoCalibrationIOBoxResult(BaseModel):
    pickup_low_x: float
    pickup_low_y: float
    pickup_high_x: float
    pickup_high_y: float
    z_extension: float
    stage_extension: float


class LaserAutoCalibrationScope(BaseModel):
    mode: Literal["all", "side", "shelf", "cell", "io_box"] = "all"
    side: Literal["left_module", "right_module"] | None = None
    shelf_idx: int | None = None
    cell_idx: int | None = None
    include_io_box: bool = False


class LaserAutoCalibrationSnapshot(BaseModel):
    state: Literal["idle", "starting", "running", "applying", "completed", "failed", "cancelled"] = "idle"
    session_id: str | None = None
    run_id: str | None = None
    requested_scope: LaserAutoCalibrationScope = Field(default_factory=LaserAutoCalibrationScope)
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
    left_stage_extension_mm: float | None = None
    right_stage_extension_mm: float | None = None
    shelf_edges: list[LaserAutoCalibrationShelfEdge] = Field(default_factory=list)
    shelf_candidates: list[LaserAutoCalibrationCandidate] = Field(default_factory=list)
    detected_cells: list[LaserAutoCalibrationCell] = Field(default_factory=list)
    angle_results: list[LaserAutoCalibrationAngleResult] = Field(default_factory=list)
    io_box_result: LaserAutoCalibrationIOBoxResult | None = None
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


def _build_default_scans_dir() -> str:
    config_path = get_config_path()

    if config_path is not None:
        return str((Path(config_path).resolve().parent / DEFAULT_SCANS_DIR_NAME).resolve())

    return str((Path("data") / DEFAULT_SCANS_DIR_NAME).resolve())


def _side_to_module_name(side: StorageModuleSide) -> Literal["left_module", "right_module"]:
    if side == StorageModuleSide.LEFT:
        return "left_module"

    return "right_module"


def _side_to_short_name(side: StorageModuleSide) -> Literal["left", "right"]:
    if side == StorageModuleSide.LEFT:
        return "left"

    return "right"


def _module_name_to_side(side_name: Literal["left_module", "right_module"]) -> StorageModuleSide:
    if side_name == "left_module":
        return StorageModuleSide.LEFT

    return StorageModuleSide.RIGHT


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
    laser_offset_x_mm = 0.0

    if side != StorageModuleSide.RIGHT:
        return samples, laser_offset_x_mm

    rotation_center_deg = _build_angle_rotation_center_deg(side)
    adjusted_samples = [
        angle_module.LaserAngleCalibrationSample(
            axis_x_mm=float(sample.axis_x_mm),
            axis_z_mm=float(sample.axis_z_mm),
            rotation_deg=-float(sample.rotation_deg),
            absolute_rotation_deg=(2.0 * rotation_center_deg) - float(sample.absolute_rotation_deg),
            stage_extension_mm=float(sample.stage_extension_mm),
            z_extension_mm=float(sample.z_extension_mm),
            distance_mm=float(sample.distance_mm),
        )
        for sample in samples
    ]

    return adjusted_samples, laser_offset_x_mm


def _build_angle_rotation_positions_for_side(
    side: StorageModuleSide,
    positions: list[float],
) -> list[float]:
    if side != StorageModuleSide.RIGHT:
        return positions

    filtered_positions = [position for position in positions if position <= 0.0]

    if len(filtered_positions) == 0:
        raise ValueError("right-side rotation sweep has no valid positions at or below center")

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


__all__ = [
    "asyncio",
    "argparse",
    "asynccontextmanager",
    "suppress",
    "dataclass",
    "field",
    "datetime",
    "timezone",
    "logging",
    "logger",
    "math",
    "Path",
    "re",
    "sys",
    "Any",
    "Literal",
    "uuid4",
    "BaseModel",
    "Field",
    "angle_module",
    "laser_module",
    "Parameters",
    "resolve_grid_path",
    "ScanBounds",
    "StorageModuleSide",
    "RobotWorker",
    "clear_config_cache",
    "get_config_path",
    "get_root_config",
    "save_root_config",
    "ACTIVE_SESSION_STATES",
    "DEFAULT_SCANS_DIR_NAME",
    "AUTO_SCAN_X_MIN_MM",
    "RIGHT_AUTO_SCAN_X_MAX_MM",
    "ANGLE_DEBUG_DIR_NAME",
    "ANGLE_CAPTURE_RE",
    "ANGLE_SAMPLE_RE",
    "ANGLE_SCOUT_SAMPLE_RE",
    "SCAN_SHELF_SCOUT_RE",
    "SCAN_SHELF_HOLE_RE",
    "SCAN_PROGRESS_SAMPLE_RE",
    "SHELF_EDGES_RE",
    "COARSE_CANDIDATES_RE",
    "SCAN_FINE_RE",
    "SCAN_FINE_RETRY_RE",
    "FINE_DETECTED_CELLS_RE",
    "ANGLE_RESULT_RE",
    "STAGE_EXTENSION_PROBE_START_MM",
    "STAGE_EXTENSION_PROBE_Y_OFFSET_MM",
    "STAGE_EXTENSION_PROBE_STEP_MM",
    "STAGE_EXTENSION_PROBE_TIMEOUT_S",
    "STAGE_EXTENSION_NO_PROGRESS_TIMEOUT_S",
    "STAGE_EXTENSION_STALL_BACKOFF_MM",
    "IO_BOX_MIN_CONFIGURED_LASER_OFFSET_X_MM",
    "_resolve_io_box_laser_offset_x_mm",
    "_should_skip_motion_cleanup_on_cancel",
    "LaserAutoCalibrationShelfEdge",
    "LaserAutoCalibrationCandidate",
    "LaserAutoCalibrationCell",
    "LaserAutoCalibrationAngleResult",
    "LaserAutoCalibrationIOBoxResult",
    "LaserAutoCalibrationScope",
    "LaserAutoCalibrationSnapshot",
    "_SideState",
    "_CompletedSideResult",
    "_get_timestamp",
    "_build_default_scans_dir",
    "_side_to_module_name",
    "_side_to_short_name",
    "_module_name_to_side",
    "_build_angle_rotation_center_deg",
    "_side_to_rotation_param_name",
    "_side_to_stage_extension_param_name",
    "_build_stage_extension_probe_y_mm",
    "_build_bounds_for_side",
    "_build_valid_rotation_positions",
    "_build_angle_x_positions",
    "_build_angle_z_offset_positions",
    "_build_estimated_rotation_deg",
    "_build_angle_fit_inputs",
    "_build_angle_rotation_positions_for_side",
    "_build_angle_calibration_sample",
    "_build_candidate_rows",
    "_build_cell_rows",
]
