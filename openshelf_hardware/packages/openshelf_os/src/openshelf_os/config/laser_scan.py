from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from openshelf_os.config.schema import reject_legacy_keys
from openshelf_os.config_migrations.v002_laser_parameter_names import (
    REMOVED_SCAN_KEYS, SCAN_KEY_RENAMES, Z_MODEL_KEY_RENAMES,
)
from openshelf_os.config_migrations.v003_remove_unused_scan_keys import (
    REMOVED_SCAN_KEYS as UNUSED_SCAN_KEYS,
)
from openshelf_os.config_migrations.v004_promote_scan_constants import (
    REMOVED_SCAN_KEYS as PROMOTED_SCAN_KEYS,
)

# Keys removed by any schema step; a current-schema config must not carry them.
REJECTED_SCAN_KEYS = (*REMOVED_SCAN_KEYS, *UNUSED_SCAN_KEYS, *PROMOTED_SCAN_KEYS)


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
    """Per-machine laser scan calibration and the tuning values machines disagree on.

    Everything the scan algorithm needs but machines agree on lives in
    `openshelf_os.laser_calibration.defaults` instead of here.
    """

    laser_offset_x_mm: Optional[float] = None
    laser_offset_y_mm: Optional[float] = None
    # Coarse scan motion. `stepped` stops and settles at every grid point; `flying` drives the
    # axis at a constant feed and bins samples taken along the way onto the same grid. Flying is
    # a POC: keep it unset until a run on the machine has been compared against a stepped one.
    coarse_scan_style: Literal["stepped", "flying"] = "stepped"
    coarse_scan_rpm: Optional[int] = Field(default=None, gt=0)
    coarse_scan_bin_mode: Literal["nearest", "median"] = "nearest"
    # Scan pose: absolute stage position and Z delta relative to Z_EXTENSION_PLACE.
    laser_scan_pose_stage_mm: Optional[float] = Field(default=None, ge=0)
    laser_scan_pose_z_delta_from_place_mm: Optional[float] = None
    shelf_hole_probe_y_offsets_mm: Optional[List[float]] = None
    fine_cell_scan_y_margin_mm: Optional[float] = Field(default=None, gt=0)
    fine_scan_step_mm: float = Field(1.0, gt=0)
    left_shelf_scout_x_mm: Optional[float] = Field(default=None, ge=0)
    right_shelf_scout_x_mm: Optional[float] = Field(default=None, ge=0)
    shelf_scout_post_edge_skip_mm: float = Field(40.0, ge=0)
    shelf_brim_max_height_mm: float = Field(13.0, gt=0)

    left_gripper_xy_correction_mm: Optional[LaserScanXYOffsetConfig] = None
    right_gripper_xy_correction_mm: Optional[LaserScanXYOffsetConfig] = None

    left_wall_depth_to_gripper_z_model: Optional[LaserScanZOffsetModelConfig] = None
    right_wall_depth_to_gripper_z_model: Optional[LaserScanZOffsetModelConfig] = None

    z_stall_anchor_enabled: bool = False
    z_stall_tray_engagement_mm: float = 0.0
    z_stall_manual_agreement_mm: float = Field(1.0, gt=0)
    # How many shelves to probe, spread across the module (3 -> bottom, middle, top). One point per
    # shelf samples the rack's own variation and validates the model shelf by shelf, where three
    # points on one shelf only measure the tool's tilt there.
    z_stall_probe_shelves: int = Field(3, ge=1)
    z_stall_probe_repeats: int = Field(2, ge=1)
    # A stall is detected at the first probe step that fails to progress, so the repeat check is
    # quantised by z_stall_probe_step_mm: a tolerance below that step rejects every point by
    # construction (0.5mm step, 0.34-0.64mm spreads measured on lb4a-2). Halve the step to tighten.
    z_stall_probe_repeat_tolerance_mm: float = Field(1.0, gt=0)
    z_stall_probe_x_shift_mm: float = 40.0

    left_bounds_x_min_mm: float = 0.0
    left_bounds_x_max_mm: float = 950.0
    left_bounds_y_min_mm: float = 0.0
    left_bounds_y_max_mm: float = 1250.0

    right_bounds_x_min_mm: float = 0.0
    right_bounds_x_max_mm: float = 950.0
    right_bounds_y_min_mm: float = 0.0
    right_bounds_y_max_mm: float = 1250.0

    angle_calibration_x_max_mm: float = 950.0

    angle_calibration_stage_stall_no_progress_timeout_s: float = Field(0.25, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_scan_keys(cls, value):
        return reject_legacy_keys(value, SCAN_KEY_RENAMES, REJECTED_SCAN_KEYS)
