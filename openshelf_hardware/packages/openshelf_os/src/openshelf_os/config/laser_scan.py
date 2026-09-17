from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.schema import reject_legacy_keys
from openshelf_os.config_migrations.v002_laser_parameter_names import (
    REMOVED_SCAN_KEYS, SCAN_KEY_RENAMES, Z_MODEL_KEY_RENAMES,
)
from openshelf_os.config_migrations.v003_remove_unused_scan_keys import (
    REMOVED_SCAN_KEYS as UNUSED_SCAN_KEYS,
)

# Keys removed by any schema step; a current-schema config must not carry them.
REJECTED_SCAN_KEYS = (*REMOVED_SCAN_KEYS, *UNUSED_SCAN_KEYS)


class LaserScanXYOffsetConfig(BaseModel):
    """Per-side translation added after the shared laser XY conversion."""

    x_mm: float
    y_mm: float


class LaserScanZOffsetModelConfig(BaseModel):
    """Turns a laser wall depth reading into a gripper z offset, without touching the rack.

    z_offset = wall_depth + intercept_mm + x_slope_mm_per_mm * grab_x

    The laser and the gripper ride the same carriage, so the carriage-to-rack distance cancels
    out of this model: what is left is the fixed laser-to-gripper geometry (intercept) and the
    tool's pitch across the x travel (slope). Both are per side and per install.

    z_offset is expressed relative to Z_EXTENSION_PLACE and is measured with the stage extended,
    so the model only holds for the stage extension and z place it was derived at. Both are
    pinned here and re-checked before the model is applied.
    """

    intercept_mm: float
    x_slope_mm_per_mm: float = 0.0
    reference_stage_mm: float
    reference_z_place_mm: float


    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_model_keys(cls, value):
        return reject_legacy_keys(value, Z_MODEL_KEY_RENAMES)


class LaserScanConfig(BaseModel):
    wall_depth_tolerance_mm: float = Field(2.0, gt=0)
    expected_hole_width_mm: float = Field(26.0, gt=0)
    expected_hole_height_mm: float = Field(30.0, gt=0)
    hole_width_tolerance_mm: float = Field(10.0, gt=0)
    hole_height_tolerance_mm: float = Field(10.0, gt=0)
    min_vertical_wall_coverage_mm: float = Field(10.0, gt=0)
    min_horizontal_wall_coverage_mm: float = Field(30.0, gt=0)
    laser_offset_x_mm: Optional[float] = None
    laser_offset_y_mm: Optional[float] = None
    # Scan pose: absolute stage position and Z delta relative to Z_EXTENSION_PLACE.
    laser_scan_pose_stage_mm: Optional[float] = Field(default=None, ge=0)
    laser_scan_pose_z_delta_from_place_mm: Optional[float] = None
    shelf_hole_probe_y_offsets_mm: Optional[List[float]] = None
    fine_cell_scan_y_margin_mm: Optional[float] = Field(default=None, gt=0)
    fine_scan_step_mm: float = Field(1.0, gt=0)
    laser_sample_settle_ms: int = Field(50, ge=0)
    laser_samples_per_point: int = Field(3, ge=1)
    left_shelf_scout_x_mm: Optional[float] = Field(default=None, ge=0)
    right_shelf_scout_x_mm: Optional[float] = Field(default=None, ge=0)
    shelf_scout_post_edge_skip_mm: float = Field(40.0, ge=0)
    shelf_brim_max_height_mm: float = Field(13.0, gt=0)

    left_gripper_xy_correction_mm: Optional[LaserScanXYOffsetConfig] = None
    right_gripper_xy_correction_mm: Optional[LaserScanXYOffsetConfig] = None

    left_wall_depth_to_gripper_z_model: Optional[LaserScanZOffsetModelConfig] = None
    right_wall_depth_to_gripper_z_model: Optional[LaserScanZOffsetModelConfig] = None
    z_offset_model_param_tolerance_mm: float = Field(0.2, gt=0)

    z_stall_anchor_enabled: bool = False
    z_stall_tray_engagement_mm: float = 0.0
    z_stall_probe_x_shift_mm: float = 40.0
    z_stall_probe_start_mm: float = 5.0
    z_stall_probe_fast_approach_mm: float = 130.0
    z_stall_probe_target_mm: float = 180.0
    z_stall_probe_step_mm: float = Field(0.5, gt=0)
    z_stall_probe_rpm: Optional[int] = Field(default=20, gt=0)
    z_stall_probe_no_progress_s: float = Field(0.25, gt=0)
    z_stall_probe_timeout_s: float = Field(60.0, gt=0)

    left_bounds_x_min_mm: float = 0.0
    left_bounds_x_max_mm: float = 950.0
    left_bounds_y_min_mm: float = 0.0
    left_bounds_y_max_mm: float = 1250.0

    right_bounds_x_min_mm: float = 0.0
    right_bounds_x_max_mm: float = 950.0
    right_bounds_y_min_mm: float = 0.0
    right_bounds_y_max_mm: float = 1250.0

    angle_calibration_x_min_mm: float = 0.0
    angle_calibration_x_max_mm: float = 950.0
    angle_calibration_x_step_mm: float = Field(100.0, gt=0)
    angle_calibration_x_count: int = Field(5, gt=0)
    angle_calibration_rotation_step_deg: float = Field(1.0, gt=0)

    angle_calibration_stage_stall_rotation_min_deg: float = 0.0
    angle_calibration_stage_stall_rotation_max_deg: float = 5.5
    angle_calibration_stage_stall_coarse_min_deg: float = 0.0
    angle_calibration_stage_stall_coarse_max_deg: float = 5.5
    angle_calibration_stage_stall_coarse_step_deg: float = Field(1.0, gt=0)
    angle_calibration_stage_stall_coarse_start_deg: Optional[float] = 4.5
    angle_calibration_stage_stall_fine_window_deg: float = Field(1.5, ge=0)
    angle_calibration_stage_stall_fine_step_deg: float = Field(0.25, gt=0)
    angle_calibration_stage_stall_fast_approach_mm: Optional[float] = Field(default=0.0, ge=0)
    angle_calibration_stage_stall_validation_count: int = Field(3, ge=0)
    angle_calibration_stage_stall_validation_repeats: int = Field(3, ge=1)
    angle_calibration_stage_stall_validation_retry_count: int = Field(1, ge=0)
    angle_calibration_stage_stall_coarse_retry_count: int = Field(1, ge=0)
    angle_calibration_stage_stall_min_valid_stage_position_mm: float = Field(40.0, ge=0)
    angle_calibration_stage_stall_validation_plateau_tolerance_mm: float = Field(0.75, ge=0)
    angle_calibration_stage_stall_coarse_validation_count: int = Field(1, ge=0)
    angle_calibration_stage_stall_validation_high_side_window_deg: float = Field(0.75, ge=0)
    angle_calibration_stage_stall_low_contact_retry_count: int = Field(1, ge=0)
    angle_calibration_stage_stall_validation_min_valid_sample_count: int = Field(2, ge=1)
    angle_calibration_stage_stall_probe_x_mm: Optional[float] = 475.0
    angle_calibration_stage_stall_start_mm: float = Field(3.0, ge=0)
    angle_calibration_stage_stall_target_mm: Optional[float] = Field(default=None, gt=0)
    angle_calibration_stage_stall_probe_step_mm: float = Field(0.5, gt=0)
    angle_calibration_stage_stall_probe_rpm: int = Field(20, gt=0)
    angle_calibration_stage_stall_fast_approach_margin_mm: float = Field(2.0, ge=0)
    angle_calibration_stage_stall_fast_approach_rpm: int = Field(20, gt=0)
    angle_calibration_stage_stall_timeout_s: float = Field(30.0, gt=0)
    angle_calibration_stage_stall_no_progress_timeout_s: float = Field(0.25, gt=0)
    angle_calibration_stage_stall_backoff_mm: float = Field(8.0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_scan_keys(cls, value):
        return reject_legacy_keys(value, SCAN_KEY_RENAMES, REJECTED_SCAN_KEYS)
