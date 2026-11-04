from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from openshelf_os import runtime_state
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.runtime_state import (
    RuntimeStatePaths,
    ensure_mode_workspace,
)


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
