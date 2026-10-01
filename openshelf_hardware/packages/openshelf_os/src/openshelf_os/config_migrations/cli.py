"""Preview or apply sequential config migrations, with validation and rollback backups.

Run from the backend workspace using its installed dependencies:
uv run --no-sync python ../scripts/migrate_config.py data [--write]
Stop the application before writing. No hardware is initialized by this command.
The server also auto-migrates the active config file on startup; use this
command to pre-migrate every mode's config file, or to work offline.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from openshelf_os.config_migrations.apply import plan_config_file, write_planned


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
        plans = [plan_config_file(path) for path in paths]
        for path, original, migrated, changes, backup_suffix in plans:
            print(
                f"{path}: {'migrate' if args.write else 'would migrate'}"
                if changes
                else f"{path}: unchanged"
            )
            for change in changes:
                print(f"  {change}")
            if args.write:
                backup = write_planned((path, original, migrated, changes, backup_suffix))
                if backup is not None:
                    print(f"  backup: {backup.name}")
        if not args.write:
            print("Preview only; stop the application, then rerun with --write to migrate.")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
