"""Schema 4 → 5: rename the storage module sides left/right to front/back.

The wire, config and storage names changed together across the whole system, so
there is no legacy acceptance: after this step the left_* scan keys and the
left_module/right_module bin side values are gone, and the current schema
rejects them.
"""

from __future__ import annotations

import copy
from typing import Any


SCAN_KEY_RENAMES = {
    "left_shelf_scout_x_mm": "front_shelf_scout_x_mm",
    "right_shelf_scout_x_mm": "back_shelf_scout_x_mm",
    "left_gripper_xy_correction_mm": "front_gripper_xy_correction_mm",
    "right_gripper_xy_correction_mm": "back_gripper_xy_correction_mm",
    "left_wall_depth_to_gripper_z_model": "front_wall_depth_to_gripper_z_model",
    "right_wall_depth_to_gripper_z_model": "back_wall_depth_to_gripper_z_model",
    "left_bounds_x_min_mm": "front_bounds_x_min_mm",
    "left_bounds_x_max_mm": "front_bounds_x_max_mm",
    "left_bounds_y_min_mm": "front_bounds_y_min_mm",
    "left_bounds_y_max_mm": "front_bounds_y_max_mm",
    "right_bounds_x_min_mm": "back_bounds_x_min_mm",
    "right_bounds_x_max_mm": "back_bounds_x_max_mm",
    "right_bounds_y_min_mm": "back_bounds_y_min_mm",
    "right_bounds_y_max_mm": "back_bounds_y_max_mm",
}
SIDE_VALUE_RENAMES = {
    "left_module": "front_module",
    "right_module": "back_module",
}


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


def _bin_home_locations(config: dict[str, Any]) -> list[dict[str, Any]]:
    storage = config.get("storage")
    if not isinstance(storage, dict):
        return []
    bins = storage.get("bins")
    if not isinstance(bins, list):
        return []
    return [
        bin["home_location"]
        for bin in bins
        if isinstance(bin, dict) and isinstance(bin.get("home_location"), dict)
    ]


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    changes: list[str] = []

    scan = _scan_block(migrated)
    if scan is not None:
        _rename_keys(scan, SCAN_KEY_RENAMES, "robot.grid.scan", changes)

    for location in _bin_home_locations(migrated):
        side = location.get("side")
        if side in SIDE_VALUE_RENAMES:
            location["side"] = SIDE_VALUE_RENAMES[side]
            changes.append(f"storage bin home_location.side {side} -> {location['side']}")

    return migrated, changes
