"""Schema 7 → 8: drop the scan keys that chose between stepped and flying laser scans.

Flying is now the only scan motion, so `coarse_scan_style` and `fine_scan_style` select
nothing. `shelf_brim_max_height_mm` was only passed to the stepped shelf scout, which
never read it. Values are dropped rather than moved because nothing reads them any more.
"""

from __future__ import annotations

import copy
from typing import Any


REMOVED_SCAN_KEYS = (
    "coarse_scan_style",
    "fine_scan_style",
    "shelf_brim_max_height_mm",
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
