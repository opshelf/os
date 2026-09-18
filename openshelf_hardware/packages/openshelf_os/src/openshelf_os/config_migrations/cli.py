"""Preview or apply sequential config migrations, with validation and rollback backups.

Run from the backend workspace using its installed dependencies:
uv run --no-sync python ../scripts/migrate_config.py data [--write]
Stop the application before writing. No hardware is initialized by this command.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any

from pydantic import ValidationError

from openshelf_os.config.root import RootConfig
from openshelf_os.config.schema import read_schema_version
from openshelf_os.config_migrations.runner import migrate_config


def validate_migrated_config(config: dict[str, Any]) -> None:
    try:
        RootConfig.model_validate(config)
    except ValidationError as exc:
        # Do not print config values: credentials may occur anywhere in the document.
        locations = [
            ".".join(str(part) for part in error["loc"]) or "root" for error in exc.errors()
        ]
        raise ValueError("Migrated config failed validation at: " + ", ".join(locations)) from None


def _load_json(raw: bytes) -> dict[str, Any]:
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


def _write_migrated(
    path: Path, original: bytes, migrated: dict[str, Any], backup_suffix: str
) -> None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Config files or data directories (config.json and config.*.json, non-recursive)",
    )
    parser.add_argument(
        "--write", action="store_true", help="Write changes with backups; default is preview only"
    )
    args = parser.parse_args(argv)
    try:
        paths: list[Path] = []
        for requested in args.paths:
            candidates = (
                sorted(
                    path
                    for path in requested.glob("config*.json")
                    if path.name == "config.json" or path.name.startswith("config.")
                )
                if requested.is_dir()
                else [requested]
            )
            if not candidates:
                raise ValueError(f"{requested}: no config*.json files found")
            for path in candidates:
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"{path}: expected a regular config file, not a symlink")
                path = path.absolute()
                if path not in paths:
                    paths.append(path)

        # Validate every requested document and backup destination before changing any file.
        plans = []
        for path in paths:
            original = path.read_bytes()
            config = _load_json(original)
            source_version = read_schema_version(config)
            migrated, changes = migrate_config(config)
            validate_migrated_config(migrated)
            backup_suffix = f".schema-v{source_version}.bak"
            if args.write and changes and path.with_name(path.name + backup_suffix).exists():
                raise ValueError(
                    f"{path}: migration backup already exists; preserve it and reconcile before retrying"
                )
            plans.append((path, original, migrated, changes, backup_suffix))
        for path, original, migrated, changes, backup_suffix in plans:
            print(
                f"{path}: {'migrate' if args.write else 'would migrate'}"
                if changes
                else f"{path}: unchanged"
            )
            for change in changes:
                print(f"  {change}")
            if args.write and changes:
                _write_migrated(path, original, migrated, backup_suffix)
                print(f"  backup: {path.name}{backup_suffix}")
        if not args.write:
            print("Preview only; stop the application, then rerun with --write to migrate.")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
