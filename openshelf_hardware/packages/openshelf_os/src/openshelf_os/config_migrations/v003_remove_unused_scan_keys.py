"""Schema 2 → 3: drop laser scan keys that no code path reads.

Every key removed here was written by the config generator and carried by every
machine, but nothing ever read it: the legacy beam-angle sweep it belonged to was
replaced by the stage-stall method. Values are dropped rather than moved because
they influenced nothing.
"""

from __future__ import annotations

import copy
from typing import Any


REMOVED_SCAN_KEYS = (
    # The angle calibration method is set to stage_stall in the model itself.
    "angle_calibration_method",
    # Legacy beam-angle sweep, superseded by the angle_calibration_stage_stall_* group.
    "angle_calibration_z_offset_min_mm",
    "angle_calibration_z_offset_max_mm",
    "angle_calibration_z_offset_step_mm",
    "angle_calibration_z_offset_count",
    "angle_calibration_rotation_min_deg",
    "angle_calibration_rotation_max_deg",
    "angle_calibration_alpha_min_deg",
    "angle_calibration_alpha_max_deg",
    "angle_calibration_coarse_step_deg",
    "angle_calibration_fine_window_deg",
    "angle_calibration_fine_step_deg",
    # Read by the shelf-stall detection helper, which never receives the configured value.
    "angle_calibration_stage_stall_fall_threshold_mm",
    "angle_calibration_stage_stall_stop_after_fall_count",
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
            changes.append(f"robot.grid.scan.{key} removed (unused)")
    return migrated, changes
