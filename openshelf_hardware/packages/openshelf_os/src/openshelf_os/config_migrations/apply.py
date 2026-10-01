"""Apply sequential config migrations to a file, with validation and rollback backups.

Shared by the offline CLI (which plans every requested file before writing any of
them) and the server's on-startup auto-migration (one file, plan and write in a
single step). A file is rewritten only after the full migration chain succeeds and
the result passes RootConfig validation; the pre-migration bytes are preserved in
an exclusively created ``.schema-v{source}.bak``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from pydantic import ValidationError

from openshelf_os.config.root import RootConfig
from openshelf_os.config.schema import read_schema_version
from openshelf_os.config_migrations.runner import migrate_config

# path, original bytes, migrated dict, changes, backup suffix
Plan = tuple[Path, bytes, dict[str, Any], list[str], str]


def load_config_json(raw: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key; reconcile before migrating")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("Non-finite JSON number; reconcile before migrating")

    config = json.loads(raw, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    if not isinstance(config, dict):
        raise ValueError("Config root must be an object")
    return config


def validate_migrated_config(config: dict[str, Any]) -> None:
    try:
        RootConfig.model_validate(config)
    except ValidationError as exc:
        # Do not print config values: credentials may occur anywhere in the document.
        locations = [
            ".".join(str(part) for part in error["loc"]) or "root" for error in exc.errors()
        ]
        raise ValueError("Migrated config failed validation at: " + ", ".join(locations)) from None


def _write_migrated(path: Path, original: bytes, migrated: dict[str, Any], backup_suffix: str) -> None:
    if path.is_symlink() or path.read_bytes() != original:
        raise ValueError(f"{path}: changed since preview; retry with the application stopped")
    source_stat = path.stat()
    backup = path.with_name(path.name + backup_suffix)
    # Exclusive creation prevents a later invocation from overwriting the rollback copy.
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        os.fchmod(stream.fileno(), stat.S_IMODE(source_stat.st_mode))
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())

    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            if (os.geteuid(), os.getegid()) != (source_stat.st_uid, source_stat.st_gid):
                os.fchown(stream.fileno(), source_stat.st_uid, source_stat.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(source_stat.st_mode))
            json.dump(migrated, stream, indent="\t", ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def plan_config_file(path: Path) -> Plan:
    """Read, migrate, and validate one config file without writing anything.

    Raises ValueError for unreadable documents, newer-than-supported versions,
    failed validation, or an existing migration backup that must be reconciled.
    """
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{path}: expected a regular config file, not a symlink")
    path = path.absolute()
    original = path.read_bytes()
    config = load_config_json(original)
    source_version = read_schema_version(config)
    migrated, changes = migrate_config(config)
    validate_migrated_config(migrated)
    backup_suffix = f".schema-v{source_version}.bak"
    if changes and path.with_name(path.name + backup_suffix).exists():
        raise ValueError(
            f"{path}: migration backup already exists; preserve it and reconcile before retrying"
        )
    return (path, original, migrated, changes, backup_suffix)


def write_planned(plan: Plan) -> Path | None:
    """Apply a planned migration; returns the backup path when a write happened."""
    path, original, migrated, changes, backup_suffix = plan
    if not changes:
        return None
    _write_migrated(path, original, migrated, backup_suffix)
    return path.with_name(path.name + backup_suffix)


def migrate_config_file(path: Path) -> tuple[bool, list[str], Path | None]:
    """Migrate one config file in place; (changed, changes, backup path)."""
    plan = plan_config_file(path)
    backup = write_planned(plan)
    return (backup is not None, plan[3], backup)
