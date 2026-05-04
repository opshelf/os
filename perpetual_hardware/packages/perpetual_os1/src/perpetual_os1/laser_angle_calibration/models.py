from __future__ import annotations

from dataclasses import dataclass

from perpetual_os1 import laser_shelf_scout as shared_shelf_scout

ShelfSurfaceEdge = shared_shelf_scout.ShelfSurfaceEdge


@dataclass
class LaserAngleCalibrationSample:
	axis_x_mm: float
	axis_z_mm: float
	rotation_deg: float
	absolute_rotation_deg: float
	stage_extension_mm: float
	z_extension_mm: float
	distance_mm: float


@dataclass
class FittedLine:
	point_x_mm: float
	point_z_mm: float
	direction_x: float
	direction_z: float
	normal_x: float
	normal_z: float
	line_angle_deg: float
	rms_residual_mm: float
	max_abs_residual_mm: float


@dataclass
class AlphaFitResult:
	alpha_deg: float
	sample_count: int
	line: FittedLine


@dataclass
class StageStallProbeSample:
	rotation_deg: float
	absolute_rotation_deg: float
	probe_x_mm: float
	probe_y_mm: float
	stage_position_mm: float
	stalled: bool
	status: str
	alarm_code: int | None
