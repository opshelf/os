"""Schema 3 → 4: move uniform laser scan tuning values from config into code.

These keys were identical on every machine, and every read site already fell back to
the same literal when the key was absent. They describe the scan algorithm, the shelf
fixtures and the scan plan, so they now live in
`openshelf_os.laser_calibration.defaults` as one source of truth.

Keys that the fleet does not agree on are deliberately left in config, along with the
per-machine calibration values.

A stored value must remain behaviour-equivalent to the constant that replaces it, or
the step refuses to migrate: dropping a value that drifted from the fleet would
silently retune that machine's scan. The shadowed fallback keys in
`SHADOWED_SCAN_KEYS` are exempt from that comparison: their stored value never
reached the sweep, so a difference from any constant says nothing about behaviour.
The old reader's null spellings keep working: null is the only spelling of
`angle_calibration_stage_stall_target_mm`, whose constant is null (probe to
the stage travel limit). A null `angle_calibration_stage_stall_fast_approach_mm`
is different: the constant is 0.0, and null derived the probe start from the
tool parameters instead, so it is refused.
"""

from __future__ import annotations

import copy
from typing import Any

import openshelf_os.laser_calibration.defaults as defaults


REMOVED_SCAN_KEYS = (
    # Hole detection.
    "wall_depth_tolerance_mm",
    "expected_hole_width_mm",
    "expected_hole_height_mm",
    "hole_width_tolerance_mm",
    "hole_height_tolerance_mm",
    "min_vertical_wall_coverage_mm",
    "min_horizontal_wall_coverage_mm",
    # Laser sampling.
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

# Keys removed as shadowed: the stage-stall sweep read
# angle_calibration_stage_stall_coarse_{min,max,step}_deg and used these only as
# their fallbacks (the pre-promotion reader resolved the coarse keys first, falling
# back to angle_calibration_stage_stall_rotation_{min,max}_deg and
# angle_calibration_rotation_step_deg), and the fleet always stored the coarse keys.
# A value an operator set here therefore never reached the algorithm, so comparing
# it against a code constant is a false positive: the key is still removed, but its
# stored value is irrelevant and must not be checked below.
SHADOWED_SCAN_KEYS = (
    "angle_calibration_stage_stall_rotation_min_deg",
    "angle_calibration_stage_stall_rotation_max_deg",
    "angle_calibration_rotation_step_deg",
)

# laser_calibration.defaults attribute holding the constant each promoted key was
# replaced with. Recording the replacement next to the key is what the
# behaviour-equivalence check below compares stored values against, so it must name
# exactly the promoted keys: REMOVED_SCAN_KEYS minus the shadowed keys, whose
# stored values are irrelevant (see SHADOWED_SCAN_KEYS).
PROMOTED_CONSTANT_ATTRS = {
    "wall_depth_tolerance_mm": "WALL_DEPTH_TOLERANCE_MM",
    "expected_hole_width_mm": "EXPECTED_HOLE_WIDTH_MM",
    "expected_hole_height_mm": "EXPECTED_HOLE_HEIGHT_MM",
    "hole_width_tolerance_mm": "HOLE_WIDTH_TOLERANCE_MM",
    "hole_height_tolerance_mm": "HOLE_HEIGHT_TOLERANCE_MM",
    "min_vertical_wall_coverage_mm": "MIN_VERTICAL_WALL_COVERAGE_MM",
    "min_horizontal_wall_coverage_mm": "MIN_HORIZONTAL_WALL_COVERAGE_MM",
    "laser_sample_settle_ms": "LASER_SAMPLE_SETTLE_MS",
    "laser_samples_per_point": "LASER_SAMPLES_PER_POINT",
    "z_offset_model_param_tolerance_mm": "Z_OFFSET_MODEL_PARAM_TOLERANCE_MM",
    "z_stall_probe_start_mm": "Z_STALL_PROBE_START_MM",
    "z_stall_probe_fast_approach_mm": "Z_STALL_PROBE_FAST_APPROACH_MM",
    "z_stall_probe_target_mm": "Z_STALL_PROBE_TARGET_MM",
    "z_stall_probe_step_mm": "Z_STALL_PROBE_STEP_MM",
    "z_stall_probe_rpm": "Z_STALL_PROBE_RPM",
    "z_stall_probe_no_progress_s": "Z_STALL_PROBE_NO_PROGRESS_S",
    "z_stall_probe_timeout_s": "Z_STALL_PROBE_TIMEOUT_S",
    "angle_calibration_x_min_mm": "ANGLE_SWEEP_X_MIN_MM",
    "angle_calibration_x_step_mm": "ANGLE_SWEEP_X_STEP_MM",
    "angle_calibration_x_count": "ANGLE_SWEEP_X_COUNT",
    "angle_calibration_stage_stall_coarse_min_deg": "STAGE_STALL_COARSE_MIN_DEG",
    "angle_calibration_stage_stall_coarse_max_deg": "STAGE_STALL_COARSE_MAX_DEG",
    "angle_calibration_stage_stall_coarse_step_deg": "STAGE_STALL_COARSE_STEP_DEG",
    "angle_calibration_stage_stall_coarse_start_deg": "STAGE_STALL_COARSE_START_DEG",
    "angle_calibration_stage_stall_fine_window_deg": "STAGE_STALL_FINE_WINDOW_DEG",
    "angle_calibration_stage_stall_fine_step_deg": "STAGE_STALL_FINE_STEP_DEG",
    "angle_calibration_stage_stall_fast_approach_mm": "STAGE_STALL_FAST_APPROACH_MM",
    "angle_calibration_stage_stall_validation_count": "STAGE_STALL_VALIDATION_COUNT",
    "angle_calibration_stage_stall_validation_repeats": "STAGE_STALL_VALIDATION_REPEATS",
    "angle_calibration_stage_stall_validation_retry_count": "STAGE_STALL_VALIDATION_RETRY_COUNT",
    "angle_calibration_stage_stall_coarse_retry_count": "STAGE_STALL_COARSE_RETRY_COUNT",
    "angle_calibration_stage_stall_min_valid_stage_position_mm": "STAGE_STALL_MIN_VALID_STAGE_POSITION_MM",
    "angle_calibration_stage_stall_validation_plateau_tolerance_mm": "STAGE_STALL_VALIDATION_PLATEAU_TOLERANCE_MM",
    "angle_calibration_stage_stall_coarse_validation_count": "STAGE_STALL_COARSE_VALIDATION_COUNT",
    "angle_calibration_stage_stall_validation_high_side_window_deg": "STAGE_STALL_VALIDATION_HIGH_SIDE_WINDOW_DEG",
    "angle_calibration_stage_stall_low_contact_retry_count": "STAGE_STALL_LOW_CONTACT_RETRY_COUNT",
    "angle_calibration_stage_stall_validation_min_valid_sample_count": "STAGE_STALL_VALIDATION_MIN_VALID_SAMPLE_COUNT",
    "angle_calibration_stage_stall_probe_x_mm": "STAGE_STALL_PROBE_X_MM",
    "angle_calibration_stage_stall_start_mm": "STAGE_STALL_START_MM",
    "angle_calibration_stage_stall_target_mm": "STAGE_STALL_TARGET_MM",
    "angle_calibration_stage_stall_probe_step_mm": "STAGE_STALL_PROBE_STEP_MM",
    "angle_calibration_stage_stall_probe_rpm": "STAGE_STALL_PROBE_RPM",
    "angle_calibration_stage_stall_fast_approach_margin_mm": "STAGE_STALL_FAST_APPROACH_MARGIN_MM",
    "angle_calibration_stage_stall_fast_approach_rpm": "STAGE_STALL_FAST_APPROACH_RPM",
    "angle_calibration_stage_stall_timeout_s": "STAGE_STALL_TIMEOUT_S",
    "angle_calibration_stage_stall_backoff_mm": "STAGE_STALL_BACKOFF_MM",
}

if (
    set(PROMOTED_CONSTANT_ATTRS) | set(SHADOWED_SCAN_KEYS) != set(REMOVED_SCAN_KEYS)
    or set(PROMOTED_CONSTANT_ATTRS) & set(SHADOWED_SCAN_KEYS)
):
    raise RuntimeError(
        "PROMOTED_CONSTANT_ATTRS and SHADOWED_SCAN_KEYS must together name exactly REMOVED_SCAN_KEYS"
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


def _stored_value_is_behaviour_equivalent(key: str, value: Any) -> bool:
    """Whether a stored value keeps the behaviour the promoted constant gives now.

    Every key must equal its constant exactly, including
    `angle_calibration_stage_stall_target_mm`, whose constant is null and
    therefore accepts only null.
    """
    constant = getattr(defaults, PROMOTED_CONSTANT_ATTRS[key])
    return value == constant


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    scan = _scan_block(migrated)
    if scan is None:
        return migrated, []

    divergent = [
        f"robot.grid.scan.{key}"
        # Shadowed fallbacks are exempt: their stored value never reached the sweep.
        for key in REMOVED_SCAN_KEYS
        if key not in SHADOWED_SCAN_KEYS
        and key in scan
        and not _stored_value_is_behaviour_equivalent(key, scan[key])
    ]
    if divergent:
        # Diagnostics carry paths and key names, never config values.
        raise ValueError(
            "Promoted scan values diverged from code constants: "
            f"{', '.join(divergent)}; reconcile before migrating"
        )

    changes: list[str] = []
    for key in REMOVED_SCAN_KEYS:
        if key in scan:
            del scan[key]
            # Shadowed keys are dropped as unread, not moved: their stored value never
            # reached the sweep, so nothing was promoted in their place.
            replacement = "unused" if key in SHADOWED_SCAN_KEYS else "moved to code"
            changes.append(f"robot.grid.scan.{key} removed ({replacement})")
    return migrated, changes
