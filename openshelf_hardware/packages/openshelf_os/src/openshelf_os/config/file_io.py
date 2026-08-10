from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _get_json_backup_root(target_path: Path) -> Path:
    resolved_path = target_path.resolve()

    for parent in (resolved_path.parent, *resolved_path.parents):
        if parent.name == "data":
            return parent

    return resolved_path.parent


def create_timestamped_json_backup(
    target_path: str | Path,
    *,
    backup_group_name: str | None = None,
    backup_mode: str | None = None,
    version_id: str | None = None,
) -> Path | None:
    resolved_path = Path(target_path).resolve()

    if resolved_path.exists() is False:
        return None

    backup_root = _get_json_backup_root(resolved_path)
    backup_dir = backup_root / (backup_group_name or resolved_path.stem)
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = version_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    mode_suffix = f".{backup_mode}" if backup_mode else ""
    backup_path = backup_dir / f"{timestamp}{mode_suffix}{resolved_path.suffix}"
    shutil.copy2(resolved_path, backup_path)

    return backup_path


def _open_atomic_temp_file(target_path: Path) -> tuple[Path, int]:
    while True:
        temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")

        try:
            temp_fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            continue

        return temp_path, temp_fd


def _sync_parent_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)

    try:
        directory_fd = os.open(path.parent, flags)
    except OSError:
        return

    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _write_json_file_atomically(
    target_path: Path,
    data: Any,
    *,
    indent: int | str | None = None,
) -> None:
    temp_path: Path | None
    temp_path, temp_fd = _open_atomic_temp_file(target_path)

    try:
        if target_path.exists() is True:
            os.fchmod(temp_fd, target_path.stat().st_mode & 0o777)

        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            temp_fd = -1
            json.dump(data, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, target_path)
        _sync_parent_directory(target_path)
        temp_path = None

    finally:
        if temp_fd != -1:
            os.close(temp_fd)

        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def write_json_file_with_backup(
    target_path: str | Path,
    data: Any,
    *,
    indent: int | str | None = None,
    backup_group_name: str | None = None,
    backup_mode: str | None = None,
    version_id: str | None = None,
) -> Path:
    resolved_path = Path(target_path).resolve()

    resolved_path.parent.mkdir(parents=True, exist_ok=True)

    create_timestamped_json_backup(
        resolved_path,
        backup_group_name=backup_group_name,
        backup_mode=backup_mode,
        version_id=version_id,
    )

    _write_json_file_atomically(resolved_path, data, indent=indent)

    return resolved_path
