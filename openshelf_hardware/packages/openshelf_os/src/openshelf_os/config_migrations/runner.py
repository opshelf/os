"""Apply every migration in order to a copy; never initialize models before migration."""

from __future__ import annotations

import copy
from typing import Any, Callable

from openshelf_os.config.schema import CURRENT_SCHEMA_VERSION, read_schema_version
from openshelf_os.config_migrations import v002_laser_parameter_names

Migration = Callable[[dict[str, Any]], tuple[dict[str, Any], list[str]]]
# Each key is the destination version; step N always migrates N-1 → N.
MIGRATIONS: dict[int, Migration] = {2: v002_laser_parameter_names.migrate}


def migrate_config(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    version = read_schema_version(config)
    if version > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"Config schema {version} is newer than supported schema {CURRENT_SCHEMA_VERSION}"
        )
    migrated = copy.deepcopy(config)
    changes: list[str] = []
    for target in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
        migration = MIGRATIONS.get(target)
        if migration is None:
            raise ValueError(f"Missing config migration {target - 1} -> {target}; no files changed")
        migrated, step_changes = migration(migrated)
        migrated["schema_version"] = target
        changes.extend(step_changes)
        changes.append(f"schema_version: {target - 1} -> {target}")
    return migrated, changes
