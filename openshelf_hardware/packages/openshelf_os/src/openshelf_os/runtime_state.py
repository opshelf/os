from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openshelf_os.operating_mode import OperatingMode, get_operating_mode


DATA_DIR_ENV = "OPENSHELF_DATA_DIR"
CONFIG_PATH_ENV = "OPENSHELF_CONFIG_PATH"
DEMO_SEEDED_FILE = ".demo_workspace_seeded"


def _mode_suffix(mode: OperatingMode) -> str:
    if mode == OperatingMode.production:
        return ""
    return f".{mode.value}"


def add_mode_suffix(path: str | Path, mode: OperatingMode) -> Path:
    resolved = Path(path)
    suffix = _mode_suffix(mode)
    if suffix == "":
        return resolved

    if resolved.stem.endswith(suffix):
        return resolved

    return resolved.with_name(f"{resolved.stem}{suffix}{resolved.suffix}")


def infer_mode_from_path(path: str | Path) -> OperatingMode:
    stem = Path(path).stem
    for mode in (OperatingMode.development, OperatingMode.demo, OperatingMode.mock):
        if stem.endswith(f".{mode.value}"):
            return mode
    return OperatingMode.production


def _resolve_relative_to(path: str | Path, parent: Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve()
    return (parent / candidate).resolve()


def _calibration_dir(data_dir: Path, mode: OperatingMode) -> Path:
    if mode == OperatingMode.production:
        return (data_dir / "calibration").resolve()
    return (data_dir / "calibration" / mode.value).resolve()


def _configured_path(config: dict[str, Any], dotted_keys: tuple[str, ...], default: str) -> str:
    current: Any = config
    for key in dotted_keys:
        if isinstance(current, dict) is False:
            return default
        current = current.get(key)
    return str(current) if isinstance(current, str) and current != "" else default


@dataclass(frozen=True)
class RuntimeStatePaths:
    mode: OperatingMode
    data_dir: Path
    config_path: Path
    database_path: Path
    calibration_dir: Path
    workspace_manifest_path: Path

    @classmethod
    def current(cls) -> "RuntimeStatePaths":
        data_dir = Path(os.getenv(DATA_DIR_ENV, "data")).resolve()
        mode = get_operating_mode(data_dir / ".mode")
        config_override = os.getenv(CONFIG_PATH_ENV)
        config_path = (
            Path(config_override).resolve()
            if config_override
            else add_mode_suffix(data_dir / "config.json", mode).resolve()
        )
        return cls(
            mode=mode,
            data_dir=data_dir,
            config_path=config_path,
            database_path=add_mode_suffix(data_dir / "base.db", mode).resolve(),
            calibration_dir=_calibration_dir(data_dir, mode),
            workspace_manifest_path=(data_dir / f".workspace.{mode.value}.json").resolve(),
        )

    @classmethod
    def for_mode(cls, mode: OperatingMode, data_dir: str | Path = "data") -> "RuntimeStatePaths":
        resolved_data_dir = Path(data_dir).resolve()
        return cls(
            mode=mode,
            data_dir=resolved_data_dir,
            config_path=add_mode_suffix(resolved_data_dir / "config.json", mode).resolve(),
            database_path=add_mode_suffix(resolved_data_dir / "base.db", mode).resolve(),
            calibration_dir=_calibration_dir(resolved_data_dir, mode),
            workspace_manifest_path=(resolved_data_dir / f".workspace.{mode.value}.json").resolve(),
        )

    def resolve_mode_owned_path(self, logical_path: str | Path, *, relative_to: Path | None = None) -> Path:
        parent = relative_to if relative_to is not None else self.config_path.parent
        return add_mode_suffix(_resolve_relative_to(logical_path, parent), self.mode).resolve()

    def resolve_storage_path(self, config: Any, *, config_path: str | Path | None = None) -> Path:
        logical_path = config.robot.grid.path
        parent = Path(config_path).resolve().parent if config_path is not None else self.config_path.parent
        return self.resolve_mode_owned_path(logical_path, relative_to=parent)


def paths_for_config_file(config_path: str | Path, *, data_dir: str | Path | None = None) -> RuntimeStatePaths:
    resolved_config = Path(config_path).resolve()
    current = RuntimeStatePaths.current()
    mode = current.mode if resolved_config == current.config_path else infer_mode_from_path(resolved_config)
    resolved_data_dir = Path(data_dir).resolve() if data_dir is not None else resolved_config.parent
    paths = RuntimeStatePaths.for_mode(mode, resolved_data_dir)
    return RuntimeStatePaths(
        mode=paths.mode,
        data_dir=paths.data_dir,
        config_path=resolved_config,
        database_path=paths.database_path,
        calibration_dir=paths.calibration_dir,
        workspace_manifest_path=paths.workspace_manifest_path,
    )


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _manifest_path_label(path: Path, data_dir: Path) -> str:
    try:
        return str(path.relative_to(data_dir))
    except ValueError:
        return str(path)


def _publish_without_overwrite(temp_path: Path, target: Path) -> bool:
    try:
        os.link(temp_path, target)
    except FileExistsError:
        return False
    return True


def _atomic_copy(source: Path, target: Path) -> bool:
    if source.exists() is False or target.exists() is True:
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temp_path)
        return _publish_without_overwrite(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)


def _sqlite_backup(source: Path, target: Path) -> bool:
    if source.exists() is False or target.exists() is True:
        return False

    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        target_connection = sqlite3.connect(temp_path)
        source_connection.backup(target_connection)
        target_connection.close()
        target_connection = None
        source_connection.close()
        source_connection = None
        shutil.copymode(source, temp_path)
        return _publish_without_overwrite(temp_path, target)
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
        temp_path.unlink(missing_ok=True)


def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _merge_manifest_entries(
    existing_manifest: dict[str, Any], field: str, entries: list[str]
) -> list[str]:
    existing_entries = existing_manifest.get(field, [])
    previous = [entry for entry in existing_entries if isinstance(entry, str)]
    return list(dict.fromkeys([*previous, *entries]))


def _invalidate_demo_seed_if_core_state_recovered(
    active: RuntimeStatePaths, copied: list[str], storage_path: Path
) -> None:
    if active.mode != OperatingMode.demo:
        return

    recovered_core_state = {
        active.database_path.name,
        _manifest_path_label(storage_path, active.data_dir),
    }
    if any(path in recovered_core_state for path in copied):
        (active.data_dir / DEMO_SEEDED_FILE).unlink(missing_ok=True)


def ensure_mode_workspace(paths: RuntimeStatePaths | None = None) -> dict[str, Any] | None:
    active = paths if paths is not None else RuntimeStatePaths.current()
    if active.mode == OperatingMode.production:
        return None

    active.data_dir.mkdir(parents=True, exist_ok=True)
    existing_manifest = _read_json_object(active.workspace_manifest_path)
    manifest_exists = active.workspace_manifest_path.exists()

    production = RuntimeStatePaths.for_mode(OperatingMode.production, active.data_dir)
    copied: list[str] = []
    adopted: list[str] = []

    if active.config_path.exists():
        adopted.append(active.config_path.name)
    elif _atomic_copy(production.config_path, active.config_path):
        copied.append(active.config_path.name)
    elif active.config_path.exists():
        adopted.append(active.config_path.name)

    production_config = _read_json_object(production.config_path)
    active_config = _read_json_object(active.config_path)
    production_grid_logical = _configured_path(production_config, ("robot", "grid", "path"), "storage.json")
    active_grid_logical = _configured_path(active_config, ("robot", "grid", "path"), production_grid_logical)
    production_storage = production.resolve_mode_owned_path(
        production_grid_logical,
        relative_to=production.config_path.parent,
    )
    active_storage = active.resolve_mode_owned_path(
        active_grid_logical,
        relative_to=active.config_path.parent,
    )

    if active.mode == OperatingMode.mock:
        # Mock inventory is exclusively database-backed and must never inherit
        # a production storage snapshot.
        pass
    elif active_storage.exists():
        adopted.append(_manifest_path_label(active_storage, active.data_dir))
    elif _atomic_copy(production_storage, active_storage):
        copied.append(_manifest_path_label(active_storage, active.data_dir))
    elif active_storage.exists():
        adopted.append(_manifest_path_label(active_storage, active.data_dir))

    if active.mode == OperatingMode.mock and active.database_path.exists() is False:
        # Alembic creates a fresh database containing only seeded auth state.
        pass
    elif active.database_path.exists():
        adopted.append(active.database_path.name)
    elif _sqlite_backup(production.database_path, active.database_path):
        copied.append(active.database_path.name)
    elif active.database_path.exists():
        adopted.append(active.database_path.name)

    production_blank_logical = _configured_path(
        production_config,
        ("robot", "io", "sizer", "detection", "blank_frame_path"),
        "data/iobox_sizer/blank_frame.png",
    )
    active_blank_logical = _configured_path(
        active_config,
        ("robot", "io", "sizer", "detection", "blank_frame_path"),
        production_blank_logical,
    )
    production_blank = production.resolve_mode_owned_path(
        production_blank_logical,
        relative_to=Path.cwd(),
    )
    active_blank = active.resolve_mode_owned_path(
        active_blank_logical,
        relative_to=Path.cwd(),
    )
    if active_blank.exists():
        adopted.append(str(active_blank))
    elif _atomic_copy(production_blank, active_blank):
        copied.append(str(active_blank))
    elif active_blank.exists():
        adopted.append(str(active_blank))

    _invalidate_demo_seed_if_core_state_recovered(active, copied, active_storage)

    if manifest_exists and not copied and not adopted:
        return existing_manifest

    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "mode": active.mode.value,
        "initialized_at": existing_manifest.get("initialized_at", now),
        "source_mode": OperatingMode.production.value,
        "copied": _merge_manifest_entries(existing_manifest, "copied", copied),
        "adopted": _merge_manifest_entries(existing_manifest, "adopted", adopted),
    }
    _write_manifest(active.workspace_manifest_path, manifest)
    return manifest
