"""Schema 3 → 4: move uniform laser scan tuning values from config into code.

These keys were identical on every machine, and every read site already fell back to
the same literal when the key was absent. They describe the scan algorithm, the shelf
fixtures and the scan plan, so they now live in
`openshelf_os.laser_calibration.defaults` as one source of truth.

Keys that the fleet does not agree on are deliberately left in config, along with the
per-machine calibration values.
"""

from __future__ import annotations

import copy
from typing import Any


REMOVED_SCAN_KEYS = (
    # Hole detection.
    "wall_depth_tolerance_mm",
    "expected_hole_width_mm",
    "expected_hole_height_mm",
    "hole_width_tolerance_mm",
    "hole_height_tolerance_mm",
    "min_vertical_wall_coverage_mm",
    "min_horizontal_wall_coverage_mm",
    # Laser sampling and fine scan.
    "fine_cell_scan_y_margin_mm",
    "laser_sample_settle_ms",
    "laser_samples_per_point",
    # Per-machine model freshness tolerance.
    "z_offset_model_param_tolerance_mm",
    # Z stall anchor probe.
    "z_stall_probe_start_mm",
    "z_stall_probe_fast_approach_mm",
    "z_stall_probe_target_mm",
    "z_stall_probe_step_mm",
    "z_stall_probe_rpm",
    "z_stall_probe_no_progress_s",
    "z_stall_probe_timeout_s",
    # Legacy beam-angle sweep.
    "angle_calibration_x_min_mm",
    "angle_calibration_x_step_mm",
    "angle_calibration_x_count",
    "angle_calibration_rotation_step_deg",
    # Read only as fallbacks that the stage-stall coarse_* keys always shadowed, so the
    # value an operator set here never reached the sweep.
    "angle_calibration_stage_stall_rotation_min_deg",
    "angle_calibration_stage_stall_rotation_max_deg",
    # Stage-stall angle calibration.
    "angle_calibration_stage_stall_coarse_min_deg",
    "angle_calibration_stage_stall_coarse_max_deg",
    "angle_calibration_stage_stall_coarse_step_deg",
    "angle_calibration_stage_stall_coarse_start_deg",
    "angle_calibration_stage_stall_fine_window_deg",
    "angle_calibration_stage_stall_fine_step_deg",
    "angle_calibration_stage_stall_fast_approach_mm",
    "angle_calibration_stage_stall_validation_count",
    "angle_calibration_stage_stall_validation_repeats",
    "angle_calibration_stage_stall_validation_retry_count",
    "angle_calibration_stage_stall_coarse_retry_count",
    "angle_calibration_stage_stall_min_valid_stage_position_mm",
    "angle_calibration_stage_stall_validation_plateau_tolerance_mm",
    "angle_calibration_stage_stall_coarse_validation_count",
    "angle_calibration_stage_stall_validation_high_side_window_deg",
    "angle_calibration_stage_stall_low_contact_retry_count",
    "angle_calibration_stage_stall_validation_min_valid_sample_count",
    "angle_calibration_stage_stall_probe_x_mm",
    "angle_calibration_stage_stall_start_mm",
    "angle_calibration_stage_stall_target_mm",
    "angle_calibration_stage_stall_probe_step_mm",
    "angle_calibration_stage_stall_probe_rpm",
    "angle_calibration_stage_stall_fast_approach_margin_mm",
    "angle_calibration_stage_stall_fast_approach_rpm",
    "angle_calibration_stage_stall_timeout_s",
    "angle_calibration_stage_stall_backoff_mm",
)


def _scan_block(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return robot.grid.scan, or None when the grid carries no scan subtree."""
    node: Any = config
    for key in ("robot", "grid", "scan"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
        if key == "grid" and isinstance(node, str):
            # GridConfig also accepts a bare storage path, which has no scan subtree.
            return None
    if not isinstance(node, dict):
        raise ValueError("robot.grid.scan must be an object when present")
    return node


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    scan = _scan_block(migrated)
    if scan is None:
        return migrated, []

    changes: list[str] = []
    for key in REMOVED_SCAN_KEYS:
        if key in scan:
            del scan[key]
            changes.append(f"robot.grid.scan.{key} removed (moved to code)")
    return migrated, changes
