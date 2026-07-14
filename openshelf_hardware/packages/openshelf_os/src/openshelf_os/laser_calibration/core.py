# ruff: noqa: F401, F403, F405
from __future__ import annotations

import argparse
import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import numpy as np
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from openshelf_os.common import AxisCoordinate
from openshelf_os.laser_calibration.scan import (
	ScanBounds,
	ScanAnalysisResult,
	ScanDetectedCell,
	ScanDetectedShelf,
	analyze_scan_capture,
	build_axis_positions,
	build_shelves_from_scan,  # noqa: F401 - re-exported for server consumption
	create_scan_capture,
)
import openshelf_os.common as common_module
import openshelf_os.robot.core as robot_module
from openshelf_os.robot import Robot
from openshelf_os.storage import StorageGrid, StorageModuleSide
from openshelf_os.laser_calibration.shelf_scout import (
	GAP_DEPTH_THRESHOLD_MM,
	RIGHT_MODULE_SHELF_SCOUT_X_MM,
	SHELF_BRIM_MAX_HEIGHT_MM,
	SHELF_BRIM_MIN_HEIGHT_MM,
	SHELF_EDGE_CONFIRMATION_COUNT,
	SHELF_EDGE_TOP_MARGIN_MM,
	SHELF_MIN_SPACING_MM,
	SHELF_SCOUT_POST_EDGE_SKIP_MM,
	SHELF_SCOUT_STEP_MM,
	SHELF_SCOUT_X_OFFSETS_MM,
	ShelfSurfaceEdge,
)

LASER_SCAN_STAGE_EXTENSION_MM = 13.0
LASER_SCAN_Z_OFFSET_MM = 10.0
COARSE_SCAN_STEP_MM = 4.0
RIGHT_MODULE_ROTATION_SAFE_X_MM = 475.0
SHELF_SWEEP_X_STEP_MM = 4.0
# Coarse sweep widths quantize to the 4mm step. Across two full hardware runs (43 confirmed
# cells) real holes never measured below 24mm, while false gaps bottomed out at 20mm -- so 22
# sits in the gap and rejects the false ones without costing a real cell. Each rejected
# candidate saves a ~1.8min fine scan.
SHELF_SWEEP_MIN_CANDIDATE_WIDTH_MM = 22.0
SHELF_HOLE_SWEEP_OFFSET_MM = 32.25
SHELF_HOLE_SWEEP_OFFSETS_MM = [16.25, SHELF_HOLE_SWEEP_OFFSET_MM]
FILLED_HOLE_MIN_DEPTH_DELTA_MM = 0.4
FINE_SCAN_MARGIN_X_MM = 10.0
FINE_SCAN_MARGIN_Y_MM = 6.0
FINE_SCAN_RETRY_MIN_Y_MARGIN_MM = 36.0
FINE_OPPOSITE_EDGE_MARGIN_MM = 5.0
FINE_OPPOSITE_EDGE_BACKTRACK_MM = 8.0
FINE_X_REFINEMENT_STEP_MM = 1.0
FINE_X_REFINEMENT_MARGIN_MM = 4.0
COARSE_WALL_SEARCH_STEPS = 2
CURRENT_SCAN_LOG_PATH: Optional[Path] = None
IO_BOX_DEFAULT_START_X_MM = 0.0
IO_BOX_DEFAULT_START_Y_MM = 1250.0
IO_BOX_DEFAULT_X_MAX_MM = 250.0
IO_BOX_INTERIOR_WIDTH_MM = 160.0
IO_BOX_COARSE_STEP_MM = 5.0
IO_BOX_FINE_STEP_MM = 1.0
IO_BOX_FINE_MARGIN_MM = 20.0
IO_BOX_LOW_Y_OFFSET_MM = 35.0
IO_BOX_HIGH_Y_OFFSET_MM = 80.0
IO_BOX_STAGE_EXTENSION_FRONT_OFFSET_MM = 5.0
IO_BOX_Z_EXTENSION_MM = 163.5
IO_BOX_MIN_POSITIVE_RUN_COUNT = 2
MAX_LASER_OFFSET_ABS_MM = 1000.0
MAX_SCAN_POINT_COUNT = 250_000


def should_skip_motion_cleanup_on_cancel() -> bool:
	_, active_exception, _ = sys.exc_info()

	if isinstance(active_exception, asyncio.CancelledError) is True:
		return True

	current_task = asyncio.current_task()

	if current_task is None:
		return False

	return current_task.cancelling() > 0


def raise_if_cancelled(should_abort: Callable[[], bool] | None = None) -> None:
	if should_abort is not None and should_abort() is True:
		raise asyncio.CancelledError()

	current_task = asyncio.current_task()

	if current_task is not None and current_task.cancelling() > 0:
		raise asyncio.CancelledError()


def validate_finite_value(value: Any, name: str) -> float:
	try:
		float_value = float(value)
	except (TypeError, ValueError) as exc:
		raise ValueError(f"{name} must be a finite number") from exc

	if math.isfinite(float_value) is False:
		raise ValueError(f"{name} must be a finite number")

	return float_value



def require_scan_config_parameter(scan_config: Any, parameter_name: str) -> float:
	configured_value = getattr(scan_config, parameter_name, None)

	if configured_value is None:
		raise ValueError(f"robot.grid.scan.{parameter_name} must be configured for laser calibration")

	return validate_finite_value(configured_value, f"robot.grid.scan.{parameter_name}")


def dimensions_match(
	actual_mm: float,
	*,
	expected_mm: float,
	tolerance_mm: float,
) -> bool:
	return (expected_mm - tolerance_mm) <= actual_mm <= (expected_mm + tolerance_mm)


def validate_laser_offset(parameter_name: str, value: float) -> None:
	if abs(value) > MAX_LASER_OFFSET_ABS_MM:
		raise ValueError(
			f"robot.grid.scan.{parameter_name} must be within "
			f"+/-{MAX_LASER_OFFSET_ABS_MM:.0f} mm"
		)



def require_laser_offsets(scan_config: Any) -> tuple[float, float]:
	laser_offset_x_mm = require_scan_config_parameter(scan_config, "laser_offset_x_mm")
	laser_offset_y_mm = require_scan_config_parameter(scan_config, "laser_offset_y_mm")
	validate_laser_offset("laser_offset_x_mm", laser_offset_x_mm)
	validate_laser_offset("laser_offset_y_mm", laser_offset_y_mm)

	return laser_offset_x_mm, laser_offset_y_mm



def validate_scan_bounds(bounds: ScanBounds, label: str = "scan bounds") -> None:
	x_min = validate_finite_value(bounds.x_min, f"{label}.x_min")
	x_max = validate_finite_value(bounds.x_max, f"{label}.x_max")
	y_min = validate_finite_value(bounds.y_min, f"{label}.y_min")
	y_max = validate_finite_value(bounds.y_max, f"{label}.y_max")

	if x_max <= x_min:
		raise ValueError(f"{label}.x_max must be greater than {label}.x_min")

	if y_max <= y_min:
		raise ValueError(f"{label}.y_max must be greater than {label}.y_min")



def validate_scan_plan(
	*,
	bounds: ScanBounds,
	step_mm: float,
	settle_ms: int,
	samples_per_point: int,
	label: str = "laser scan",
	should_validate_point_count: bool = True,
) -> None:
	validate_scan_bounds(bounds)
	validated_step_mm = validate_finite_value(step_mm, f"{label} step_mm")

	if validated_step_mm <= 0.0:
		raise ValueError(f"{label} step_mm must be greater than zero")

	if settle_ms < 0:
		raise ValueError(f"{label} settle_ms must be zero or greater")

	if samples_per_point <= 0:
		raise ValueError(f"{label} samples_per_point must be greater than zero")

	if should_validate_point_count is False:
		return

	x_count = int(math.floor(abs(bounds.x_max - bounds.x_min) / validated_step_mm)) + 2
	y_count = int(math.floor(abs(bounds.y_max - bounds.y_min) / validated_step_mm)) + 2
	total_points = x_count * y_count

	if total_points > MAX_SCAN_POINT_COUNT:
		raise ValueError(
			f"{label} would scan {total_points} points; reduce bounds or increase step_mm "
			f"(maximum {MAX_SCAN_POINT_COUNT})"
		)



def get_laser_scan_stage_extension_mm(scan_config: Any) -> float:
	configured_value = getattr(scan_config, "laser_scan_stage_extension_mm", None)

	if configured_value is None:
		return LASER_SCAN_STAGE_EXTENSION_MM

	return validate_finite_value(configured_value, "robot.grid.scan.laser_scan_stage_extension_mm")


def get_laser_scan_z_offset_from_place_mm(scan_config: Any) -> float:
	configured_value = getattr(
		scan_config,
		"laser_scan_z_offset_from_place_mm",
		None,
	)

	if configured_value is None:
		return LASER_SCAN_Z_OFFSET_MM

	return validate_finite_value(
		configured_value,
		"robot.grid.scan.laser_scan_z_offset_from_place_mm",
	)


def get_laser_sample_settle_ms(scan_config: Any) -> int:
	return int(scan_config.laser_sample_settle_ms)


def get_laser_samples_per_point(scan_config: Any) -> int:
	return int(scan_config.laser_samples_per_point)


def get_scan_xy_offset(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> tuple[float, float] | None:
	prefix = "left" if side == StorageModuleSide.LEFT else "right"
	configured_offset = getattr(scan_config, f"{prefix}_xy_offset", None)

	if configured_offset is None:
		return None

	offset_x_mm = validate_finite_value(configured_offset.x_mm, f"robot.grid.scan.{prefix}_xy_offset.x_mm")
	offset_y_mm = validate_finite_value(configured_offset.y_mm, f"robot.grid.scan.{prefix}_xy_offset.y_mm")

	return offset_x_mm, offset_y_mm


def get_scan_z_offset_model(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> Any | None:
	prefix = "left" if side == StorageModuleSide.LEFT else "right"
	configured_model = getattr(scan_config, f"{prefix}_z_offset_model", None)

	if configured_model is None:
		return None

	validate_finite_value(configured_model.intercept_mm, f"robot.grid.scan.{prefix}_z_offset_model.intercept_mm")
	validate_finite_value(configured_model.slope_per_mm, f"robot.grid.scan.{prefix}_z_offset_model.slope_per_mm")
	validate_finite_value(
		configured_model.stage_extension_mm,
		f"robot.grid.scan.{prefix}_z_offset_model.stage_extension_mm",
	)
	validate_finite_value(configured_model.z_place_mm, f"robot.grid.scan.{prefix}_z_offset_model.z_place_mm")

	return configured_model


def require_z_offset_model_matches_tool(
	*,
	model: Any,
	side: StorageModuleSide,
	stage_extension_mm: float,
	z_place_mm: float,
	tolerance_mm: float,
) -> None:
	"""A z offset is relative to Z_EXTENSION_PLACE and measured at a given stage extension.

	Either drifting shifts every cell's z offset one-for-one, and the auto calibration itself
	re-derives stage extension each run, so a stale model would silently mis-place every cell.
	"""
	prefix = "left" if side == StorageModuleSide.LEFT else "right"
	drifted: list[str] = []

	if abs(float(model.stage_extension_mm) - float(stage_extension_mm)) > tolerance_mm:
		drifted.append(
			f"stage extension {float(model.stage_extension_mm):.2f}mm -> {float(stage_extension_mm):.2f}mm"
		)

	if abs(float(model.z_place_mm) - float(z_place_mm)) > tolerance_mm:
		drifted.append(f"Z_EXTENSION_PLACE {float(model.z_place_mm):.2f}mm -> {float(z_place_mm):.2f}mm")

	if len(drifted) == 0:
		return

	raise ValueError(
		f"robot.grid.scan.{prefix}_z_offset_model was derived at a different tool geometry "
		f"({', '.join(drifted)}). Every cell's z offset would be wrong by that difference. "
		"Re-derive the model or restore the tool parameters."
	)


def require_scan_xy_correction(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> None:
	if get_scan_xy_offset(scan_config=scan_config, side=side) is not None:
		return

	prefix = "left" if side == StorageModuleSide.LEFT else "right"
	raise ValueError(
		f"robot.grid.scan.{prefix}_xy_offset must be configured for laser calibration"
	)


def validate_laser_structure_parameters(
	*,
	scan_config: Any,
	side: StorageModuleSide,
) -> None:
	require_laser_offsets(scan_config)
	require_scan_xy_correction(scan_config=scan_config, side=side)


def get_shelf_hole_probe_y_offsets_mm(scan_config: Any) -> list[float]:
	configured_offsets = getattr(scan_config, "shelf_hole_probe_y_offsets_mm", None)

	if configured_offsets is None:
		return list(SHELF_HOLE_SWEEP_OFFSETS_MM)

	if len(configured_offsets) == 0:
		return list(SHELF_HOLE_SWEEP_OFFSETS_MM)

	return [
		float(offset_mm)
		for offset_mm in configured_offsets
	]


def get_shelf_scout_post_edge_skip_mm(scan_config: Any) -> float:
	configured_skip_mm = getattr(scan_config, "shelf_scout_post_edge_skip_mm", None)

	if configured_skip_mm is None:
		return SHELF_SCOUT_POST_EDGE_SKIP_MM

	validated_skip_mm = validate_finite_value(
		configured_skip_mm,
		"robot.grid.scan.shelf_scout_post_edge_skip_mm",
	)

	if validated_skip_mm < 0.0:
		raise ValueError("robot.grid.scan.shelf_scout_post_edge_skip_mm must be zero or greater")

	return validated_skip_mm


def get_shelf_brim_max_height_mm(scan_config: Any) -> float:
	configured_max_height_mm = getattr(scan_config, "shelf_brim_max_height_mm", None)

	if configured_max_height_mm is None:
		return SHELF_BRIM_MAX_HEIGHT_MM

	validated_max_height_mm = validate_finite_value(
		configured_max_height_mm,
		"robot.grid.scan.shelf_brim_max_height_mm",
	)

	if validated_max_height_mm <= 0.0:
		raise ValueError("robot.grid.scan.shelf_brim_max_height_mm must be greater than zero")

	return validated_max_height_mm


def get_fine_cell_scan_y_margin_mm(scan_config: Any) -> float:
	configured_margin_mm = getattr(scan_config, "fine_cell_scan_y_margin_mm", None)

	if configured_margin_mm is None:
		return FINE_SCAN_MARGIN_Y_MM

	return float(configured_margin_mm)


@dataclass
class GridScanResult:
	x_positions: list[float]
	y_positions: list[float]
	depth_rows: list[list[float | None]]


@dataclass
class GapCandidateRegion:
	x_min: float
	x_max: float
	y_min: float
	y_max: float
	row_count: int = 1
	wall_depth_mm: float | None = None
	wall_sample_count: int = 0




@dataclass
class LineGapSegment:
	start_idx: int
	end_idx: int
	start_mm: float
	end_mm: float
	width_mm: float
	center_mm: float


@dataclass
class StagedScanRecorder:
	run_id: str
	run_dir: Path
	coarse_depth_path: Path
	metadata_path: Path
	run_parameters_path: Path
	progress_path: Path
	log_path: Path


@dataclass
class SingleCellRescanTarget:
	shelf_idx: int
	cell_idx: int


@dataclass
class IOBoxScoutResult:
	rough_y_min_mm: float
	rough_y_max_mm: float
	fine_y_min_mm: float
	fine_y_max_mm: float
	measured_x_min_mm: float | None
	measured_x_max_mm: float
	derived_x_min_mm: float
	laser_center_x_mm: float
	laser_center_y_mm: float
	axis_center_x_mm: float
	axis_center_y_mm: float
	laser_offset_x_mm: float
	laser_offset_y_mm: float
	calibration_low_x_mm: float
	calibration_low_y_mm: float
	calibration_high_x_mm: float
	calibration_high_y_mm: float
	io_box_interior_width_mm: float
	stage_extension_mm: float
	z_extension_mm: float
	final_y_refined: bool



def get_current_timestamp() -> str:
	return datetime.now(timezone.utc).isoformat()


def depth_to_label(depth_mm: float | None) -> str:
	if depth_mm is None:
		return "none"

	return f"{depth_mm:.2f}"


def is_gap_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm <= GAP_DEPTH_THRESHOLD_MM


def is_positive_depth(depth_mm: float | None) -> bool:
	if depth_mm is None:
		return False

	return depth_mm > GAP_DEPTH_THRESHOLD_MM


def is_filled_hole_depth(
	depth_mm: float | None,
	*,
	wall_depth_mm: float,
) -> bool:
	if is_positive_depth(depth_mm) is False:
		return False

	return float(depth_mm) >= wall_depth_mm + FILLED_HOLE_MIN_DEPTH_DELTA_MM


def is_hole_interior_depth(
	depth_mm: float | None,
	*,
	wall_depth_mm: float,
) -> bool:
	if is_gap_depth(depth_mm) is True:
		return True

	return is_filled_hole_depth(
		depth_mm,
		wall_depth_mm=wall_depth_mm,
	)


def is_wall_like_depth(
	depth_mm: float | None,
	*,
	wall_depth_mm: float,
	tolerance_mm: float,
) -> bool:
	if is_positive_depth(depth_mm) is False:
		return False

	return abs(float(depth_mm) - wall_depth_mm) <= tolerance_mm


def estimate_wall_depth_mm(depth_rows: list[list[float | None]]) -> float:
	valid_values: list[float] = []

	for row in depth_rows:
		for depth_mm in row:
			if is_positive_depth(depth_mm) is False:
				continue

			valid_values.append(float(depth_mm))

	if len(valid_values) == 0:
		raise ValueError("No positive laser readings captured")

	valid_array = np.array(valid_values, dtype=np.float32)
	bin_size_mm = 0.5
	min_depth_mm = float(np.floor(np.min(valid_array) / bin_size_mm) * bin_size_mm)
	max_depth_mm = float(np.ceil(np.max(valid_array) / bin_size_mm) * bin_size_mm) + bin_size_mm
	bins = np.arange(min_depth_mm, max_depth_mm + bin_size_mm, bin_size_mm, dtype=np.float32)

	if bins.size < 2:
		return float(np.median(valid_array))

	histogram, edges = np.histogram(valid_array, bins=bins)
	dominant_idx = int(np.argmax(histogram))
	lower_bound_mm = float(edges[dominant_idx])
	upper_bound_mm = float(edges[dominant_idx + 1])
	dominant_values = valid_array[
		(valid_array >= lower_bound_mm) &
		(valid_array < upper_bound_mm)
	]

	if dominant_values.size == 0:
		return float(np.median(valid_array))

	return float(np.median(dominant_values))


def count_valid_points(depth_rows: list[list[float | None]]) -> int:
	count = 0

	for row in depth_rows:
		for depth_mm in row:
			if depth_mm is None:
				continue

			count += 1

	return count


def print_status(message: str) -> None:
	if CURRENT_SCAN_LOG_PATH is not None:
		timestamp = datetime.now(timezone.utc).isoformat()
		with CURRENT_SCAN_LOG_PATH.open("a", encoding="utf-8") as log_file:
			log_file.write(f"{timestamp}\t{message}\n")

	print(message, flush=True)


def build_standalone_config(config) -> dict:
	config_data = config.model_dump(mode="json")
	config_data["robot"]["io"]["curtain"]["enabled"] = False

	tool_config = config_data["robot"]["tool"]
	tool_config.setdefault("laser", {})
	tool_config["laser"]["enabled"] = True

	robot_bus = config_data["robot"].setdefault("bus", {})
	laser_bus = robot_bus.get("laser")
	scale_bus = robot_bus.get("scale")
	scale_enabled = config_data["robot"]["io"].get("scale", {}).get("enabled", True)

	if laser_bus is None and scale_enabled is False and scale_bus is not None:
		robot_bus["laser"] = dict(scale_bus)
		print_status("config=using scale bus for laser")

	return config_data


def load_grid(grid_path: Path) -> StorageGrid:
	if grid_path.exists() is False:
		return StorageGrid(filename=str(grid_path))

	return StorageGrid.load_from_file(str(grid_path))


async def run_with_temp_robot_config(
	*,
	config: Any,
	config_path: str,
	didRun: Callable[[Robot], Any],
	shutdown_action: str,
) -> Any:
	with tempfile.NamedTemporaryFile(
		mode="w",
		suffix=".json",
		prefix="laser-script-config-",
		dir=str(Path(config_path).resolve().parent),
		delete=False,
	) as temp_config_file:
		temp_config_path = Path(temp_config_file.name)
		temp_config_data = build_standalone_config(config)
		json.dump(temp_config_data, temp_config_file, indent="\t")

	original_common_config_path = common_module.CONFIG_PATH
	original_robot_config_path = robot_module.CONFIG_PATH

	common_module.CONFIG_PATH = temp_config_path
	robot_module.CONFIG_PATH = temp_config_path

	try:
		robot = Robot()
		print_status("phase=robot-configure")
		await robot.configure()

		try:
			return await didRun(robot)
		finally:
			if should_skip_motion_cleanup_on_cancel() is False:
				if shutdown_action == "stop":
					print_status("phase=robot-stop")
					await robot.stop()
				else:
					print_status("phase=robot-shutdown")
					await robot.shutdown()
	finally:
		common_module.CONFIG_PATH = original_common_config_path
		robot_module.CONFIG_PATH = original_robot_config_path
		temp_config_path.unlink(missing_ok=True)
