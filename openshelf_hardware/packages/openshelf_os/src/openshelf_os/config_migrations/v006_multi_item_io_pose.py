"""Schema 5 → 6: give multi-item trays their own IO box pose.

Tray delivery and return used to borrow the pickup low position and the shared IO
box stage/Z extensions. The new multi-item keys are seeded from those values so
every machine keeps its current tray behavior until it is recalibrated.
"""

from __future__ import annotations

import copy
from typing import Any


SEEDED_KEYS = {
    "PICKUP_POSITION_MULTI_ITEM": "PICKUP_POSITION_L",
    "STAGE_EXTENSION_IO_BOX_MULTI_ITEM": "STAGE_EXTENSION_IO_BOX",
    "Z_EXTENSION_IO_MULTI_ITEM": "Z_EXTENSION_IO",
}


def _parameters_block(config: dict[str, Any]) -> dict[str, Any] | None:
    """Return robot.tool.parameters, or None when the config carries no tool parameters."""
    node: Any = config
    for key in ("robot", "tool", "parameters"):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    if not isinstance(node, dict):
        raise ValueError("robot.tool.parameters must be an object when present")
    return node


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    parameters = _parameters_block(migrated)
    if parameters is None:
        return migrated, []

    changes: list[str] = []
    for new, source in SEEDED_KEYS.items():
        if new in parameters or source not in parameters:
            continue
        parameters[new] = copy.deepcopy(parameters[source])
        changes.append(f"robot.tool.parameters.{new} seeded from {source}")
    return migrated, changes
