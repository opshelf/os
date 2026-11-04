"""Schema 6 → 7: drop the z contact-probe keys the grip-edge calibration made unused.

The z stall probe that pressed the tool against the shelf plate was replaced by the
grip-edge sweep, which reads none of these. Values are dropped rather than moved
because nothing reads them any more. `z_stall_anchor_enabled` and
`z_stall_manual_agreement_mm` stay: they now switch on and gate the grip-edge sweep.
"""

from __future__ import annotations

import copy
from typing import Any


REMOVED_SCAN_KEYS = (
    "z_stall_tray_engagement_mm",
    "z_stall_probe_shelves",
    "z_stall_probe_repeats",
    "z_stall_probe_repeat_tolerance_mm",
    "z_stall_probe_x_shift_mm",
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
