"""Schema 8 → 9: fold the legacy enabled flags into the sections that own them.

`robot.enabled` became `robot_app.enabled` and `robot.io.curtain_enabled` became
`robot.io.curtain.enabled`. The models still read the old keys, but only while the new
section is absent, so a write that created the new section silently dropped the old value.
This step applies the same rule the models apply: the legacy value moves only when the new
section is absent, and is dropped when the new section exists (it was ignored already).
The effective config is identical before and after.
"""

from __future__ import annotations

import copy
from typing import Any


def migrate(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a migrated copy and changed key paths, preserving unrelated/unknown fields."""
    migrated = copy.deepcopy(config)
    changes: list[str] = []

    robot = migrated.get("robot")
    if isinstance(robot, dict) and "enabled" in robot:
        enabled = robot.pop("enabled")
        if "robot_app" not in migrated and enabled is not None:
            migrated["robot_app"] = {"enabled": enabled}
            changes.append("robot.enabled moved to robot_app.enabled")
        else:
            changes.append("robot.enabled removed (superseded by robot_app)")

    io = robot.get("io") if isinstance(robot, dict) else None
    if isinstance(io, dict) and "curtain_enabled" in io:
        enabled = io.pop("curtain_enabled")
        if "curtain" not in io:
            io["curtain"] = {"enabled": enabled}
            changes.append("robot.io.curtain_enabled moved to robot.io.curtain.enabled")
        else:
            changes.append("robot.io.curtain_enabled removed (superseded by robot.io.curtain)")

    return migrated, changes
