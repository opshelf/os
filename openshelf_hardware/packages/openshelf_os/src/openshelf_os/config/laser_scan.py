from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


class LaserScanXYReferenceConfig(BaseModel):
    shelf_idx: int = Field(ge=0)
    cell_idx: int = Field(ge=0)
    anchor: Literal["top_right", "top_left", "bottom_right", "bottom_left"] = "top_right"
    x_mm: float
    y_mm: float


class LaserScanZReferenceConfig(BaseModel):
    shelf_idx: int = Field(ge=0)
    cell_idx: int = Field(ge=0)
    z_offset_mm: float
    stage_extension_mm: float = Field(43.0, ge=0)


class LaserScanReferenceConfig(LaserScanXYReferenceConfig):
    z_offset_mm: float
    stage_extension_mm: float = Field(43.0, ge=0)
    wall_depth_mm: Optional[float] = None


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
    z_extension_mm: Optional[float] = None
    laser_scan_stage_extension_mm: Optional[float] = Field(default=None, ge=0)
    laser_scan_z_offset_from_place_mm: Optional[float] = None
    shelf_hole_probe_y_offsets_mm: Optional[List[float]] = None
    fine_cell_scan_y_margin_mm: Optional[float] = Field(default=None, gt=0)
    fine_scan_step_mm: float = Field(2.0, gt=0)
    laser_sample_settle_ms: int = Field(50, ge=0)
    laser_samples_per_point: int = Field(3, ge=1)

    left_xy_reference: Optional[LaserScanXYReferenceConfig] = None
    right_xy_reference: Optional[LaserScanXYReferenceConfig] = None
    left_z_reference: Optional[LaserScanZReferenceConfig] = None
    right_z_reference: Optional[LaserScanZReferenceConfig] = None
    left_reference: Optional[LaserScanReferenceConfig] = None
    right_reference: Optional[LaserScanReferenceConfig] = None

    left_bounds_x_min_mm: float = 0.0
    left_bounds_x_max_mm: float = 950.0
    left_bounds_y_min_mm: float = 0.0
    left_bounds_y_max_mm: float = 1250.0

    right_bounds_x_min_mm: float = 0.0
    right_bounds_x_max_mm: float = 950.0
    right_bounds_y_min_mm: float = 0.0
    right_bounds_y_max_mm: float = 1250.0

    angle_calibration_method: Literal["stage_stall"] = "stage_stall"

    angle_calibration_x_min_mm: float = 0.0
    angle_calibration_x_max_mm: float = 950.0
    angle_calibration_x_step_mm: float = Field(100.0, gt=0)
    angle_calibration_x_count: int = Field(5, gt=0)
    angle_calibration_z_offset_min_mm: float = -8.0
    angle_calibration_z_offset_max_mm: float = 8.0
    angle_calibration_z_offset_step_mm: float = Field(4.0, gt=0)
    angle_calibration_z_offset_count: int = Field(1, gt=0)
    angle_calibration_rotation_min_deg: float = -8.0
    angle_calibration_rotation_max_deg: float = 8.0
    angle_calibration_rotation_step_deg: float = Field(1.0, gt=0)
    angle_calibration_beam_zero_deg: float = 0.0
    angle_calibration_alpha_min_deg: float = -10.0
    angle_calibration_alpha_max_deg: float = 10.0
    angle_calibration_coarse_step_deg: float = Field(0.1, gt=0)
    angle_calibration_fine_window_deg: float = Field(0.3, gt=0)
    angle_calibration_fine_step_deg: float = Field(0.005, gt=0)

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
    angle_calibration_stage_stall_fall_threshold_mm: float = Field(0.15, ge=0)
    angle_calibration_stage_stall_stop_after_fall_count: int = Field(1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_scan_keys(cls, value):
        if isinstance(value, dict) is False:
            return value

        legacy_field_names = {
            "step_mm": "fine_scan_step_mm",
            "settle_ms": "laser_sample_settle_ms",
            "samples_per_point": "laser_samples_per_point",
            "scan_stage_extension_mm": "laser_scan_stage_extension_mm",
            "scan_z_offset_mm": "laser_scan_z_offset_from_place_mm",
            "shelf_hole_sweep_offsets_mm": "shelf_hole_probe_y_offsets_mm",
            "fine_scan_margin_y_mm": "fine_cell_scan_y_margin_mm",
        }

        configured_legacy_names = [
            legacy_name
            for legacy_name in legacy_field_names
            if legacy_name in value
        ]

        if len(configured_legacy_names) == 0:
            return value

        replacement_names = [
            f"{legacy_name}->{legacy_field_names[legacy_name]}"
            for legacy_name in configured_legacy_names
        ]

        replacement_label = ", ".join(replacement_names)

        raise ValueError(f"Legacy laser scan config keys are not supported: {replacement_label}")
