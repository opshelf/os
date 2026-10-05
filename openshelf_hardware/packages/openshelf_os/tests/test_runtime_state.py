from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from openshelf_os import runtime_state
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.runtime_state import (
    RuntimeStatePaths,
    ensure_mode_workspace,
)


def test_mock_workspace_never_copies_production_inventory(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text(
        '{"robot": {"grid": {"path": "storage.json"}}}', encoding="utf-8"
    )
    (data_dir / "storage.json").write_text('{"production": true}', encoding="utf-8")
    with sqlite3.connect(data_dir / "base.db") as connection:
        connection.execute("CREATE TABLE items (barcode TEXT)")
        connection.execute("INSERT INTO items VALUES ('production-item')")

    mock = RuntimeStatePaths.for_mode(OperatingMode.mock, data_dir)
    manifest = ensure_mode_workspace(mock)

    assert mock.config_path.exists()
    assert mock.database_path.exists() is False
    assert (data_dir / "storage.mock.json").exists() is False
    assert manifest is not None


def test_first_non_production_start_forks_legacy_production_workspace(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    blank_path = data_dir / "iobox_sizer/blank_frame.png"
    blank_path.parent.mkdir()
    blank_path.write_bytes(b"production-blank")
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "robot": {
                    "grid": {"path": "storage.json"},
                    "io": {"sizer": {"detection": {"blank_frame_path": str(blank_path)}}},
                }
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "storage.json").write_text('{"production": true}', encoding="utf-8")

    production_db = data_dir / "base.db"
    source = sqlite3.connect(production_db)
    source.execute("PRAGMA journal_mode=WAL")
    source.execute("CREATE TABLE state (value TEXT NOT NULL)")
    source.execute("INSERT INTO state VALUES ('production')")
    source.commit()

    development = RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)
    manifest = ensure_mode_workspace(development)

    assert manifest is not None
    assert development.config_path.read_bytes() == (data_dir / "config.json").read_bytes()
    assert (data_dir / "storage.development.json").read_text(encoding="utf-8") == '{"production": true}'
    assert (data_dir / "iobox_sizer/blank_frame.development.png").read_bytes() == b"production-blank"
    with sqlite3.connect(development.database_path) as cloned:
        assert cloned.execute("SELECT value FROM state").fetchone() == ("production",)

    source.close()


def test_development_changes_survive_restart_without_changing_production(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    production_config = data_dir / "config.json"
    production_storage = data_dir / "storage.json"
    production_database = data_dir / "base.db"
    production_config.write_text('{"robot": {"grid": {"path": "storage.json"}}}', encoding="utf-8")
    production_storage.write_text('{"workspace": "production"}', encoding="utf-8")
    with sqlite3.connect(production_database) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('production')")

    development = RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)
    ensure_mode_workspace(development)
    development.config_path.write_text('{"workspace": "development"}', encoding="utf-8")
    development_storage = data_dir / "storage.development.json"
    development_storage.write_text('{"workspace": "development"}', encoding="utf-8")
    with sqlite3.connect(development.database_path) as connection:
        connection.execute("UPDATE state SET value = 'development'")

    ensure_mode_workspace(development)
    ensure_mode_workspace(RuntimeStatePaths.for_mode(OperatingMode.production, data_dir))

    assert production_config.read_text(encoding="utf-8") == (
        '{"robot": {"grid": {"path": "storage.json"}}}'
    )
    assert production_storage.read_text(encoding="utf-8") == '{"workspace": "production"}'
    with sqlite3.connect(production_database) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("production",)
    assert development.config_path.read_text(encoding="utf-8") == '{"workspace": "development"}'
    assert development_storage.read_text(encoding="utf-8") == '{"workspace": "development"}'
    with sqlite3.connect(development.database_path) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("development",)


def test_existing_mode_workspace_is_never_overwritten(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"source": "production"}', encoding="utf-8")
    (data_dir / "config.development.json").write_text('{"source": "development"}', encoding="utf-8")

    development = RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)
    manifest = ensure_mode_workspace(development)

    assert manifest is not None
    assert development.config_path.read_text(encoding="utf-8") == '{"source": "development"}'
    assert development.config_path.name in manifest["adopted"]


def test_first_fork_does_not_overwrite_a_concurrently_created_config(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"source": "production"}', encoding="utf-8")
    development = RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)
    original_link = os.link

    def create_target_before_publish(source: Path, target: Path, *args, **kwargs) -> None:
        if Path(target) == development.config_path:
            development.config_path.write_text('{"source": "concurrent"}', encoding="utf-8")
        original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(runtime_state.os, "link", create_target_before_publish)

    manifest = ensure_mode_workspace(development)

    assert development.config_path.read_text(encoding="utf-8") == '{"source": "concurrent"}'
    assert manifest is not None
    assert development.config_path.name in manifest["adopted"]
    assert development.config_path.name not in manifest["copied"]
    assert list(data_dir.glob(".config.development.json.*.tmp")) == []


def test_production_workspace_is_not_migrated_or_modified(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = data_dir / "config.json"
    config.write_text('{"production": true}', encoding="utf-8")

    result = ensure_mode_workspace(RuntimeStatePaths.for_mode(OperatingMode.production, data_dir))

    assert result is None
    assert config.read_text(encoding="utf-8") == '{"production": true}'
    assert list(data_dir.iterdir()) == [config]
