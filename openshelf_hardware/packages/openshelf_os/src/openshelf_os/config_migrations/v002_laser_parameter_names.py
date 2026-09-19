"""Schema 1 → 2: clarify laser field names without changing calibration values."""

from __future__ import annotations

import copy
from typing import Any


SCAN_KEY_RENAMES = {
    "laser_scan_stage_extension_mm": "laser_scan_pose_stage_mm",
    "laser_scan_z_offset_from_place_mm": "laser_scan_pose_z_delta_from_place_mm",
    "left_xy_offset": "left_gripper_xy_correction_mm",
    "right_xy_offset": "right_gripper_xy_correction_mm",
    "left_z_offset_model": "left_wall_depth_to_gripper_z_model",
    "right_z_offset_model": "right_wall_depth_to_gripper_z_model",
    # Names already rejected by the previous schema, accepted by this offline migration.
    "step_mm": "fine_scan_step_mm",
    "settle_ms": "laser_sample_settle_ms",
    "samples_per_point": "laser_samples_per_point",
    "scan_stage_extension_mm": "laser_scan_pose_stage_mm",
    "scan_z_offset_mm": "laser_scan_pose_z_delta_from_place_mm",
    "shelf_hole_sweep_offsets_mm": "shelf_hole_probe_y_offsets_mm",
    "fine_scan_margin_y_mm": "fine_cell_scan_y_margin_mm",
}
Z_MODEL_KEY_RENAMES = {
    "stage_extension_mm": "reference_stage_mm",
    "z_place_mm": "reference_z_place_mm",
    "slope_per_mm": "x_slope_mm_per_mm",
}
REMOVED_SCAN_KEYS = ("z_extension_mm", "angle_calibration_beam_zero_deg")


def _rename_keys(
    value: dict[str, Any], renames: dict[str, str], path: str, changes: list[str]
) -> None:
    for old, new in renames.items():
        if old not in value:
            continue
        if new in value and value[new] != value[old]:
            raise ValueError(
                f"Conflicting keys: {path}.{old} and {path}.{new}; reconcile before migrating"
            )
        value[new] = value.pop(old)
        changes.append(f"{path}.{old} -> {new}")


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    scan = migrated
    for key in ("robot", "grid", "scan"):
        if key == "scan" and isinstance(scan, str):
            # GridConfig also accepts a bare storage path, which has no scan subtree.
            return migrated, []
        if not isinstance(scan, dict):
            raise ValueError("robot.grid.scan must be an object when present")
        if key not in scan:
            return migrated, []
        scan = scan[key]
    if not isinstance(scan, dict):
        raise ValueError("robot.grid.scan must be an object when present")

    changes: list[str] = []
    # Normalize both containers first so matching old/new models can be safely deduplicated.
    for side in ("left", "right"):
        for key in (f"{side}_z_offset_model", f"{side}_wall_depth_to_gripper_z_model"):
            model = scan.get(key)
            if model is None:
                continue
            if not isinstance(model, dict):
                raise ValueError(f"robot.grid.scan.{key} must be an object or null")
            _rename_keys(model, Z_MODEL_KEY_RENAMES, f"robot.grid.scan.{key}", changes)
    _rename_keys(scan, SCAN_KEY_RENAMES, "robot.grid.scan", changes)
    for key in REMOVED_SCAN_KEYS:
        if key in scan:
            del scan[key]
            changes.append(f"robot.grid.scan.{key} removed (unused)")
    return migrated, changes
