from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from openshelf_os import runtime_state
from openshelf_os.operating_mode import OperatingMode
from openshelf_os.runtime_state import (
    RuntimeStatePaths,
    add_mode_suffix,
    ensure_mode_workspace,
    infer_mode_from_path,
    paths_for_config_file,
)


def test_mode_variant_names_preserve_production_convention(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"

    production = RuntimeStatePaths.for_mode(OperatingMode.production, data_dir)
    development = RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)
    demo = RuntimeStatePaths.for_mode(OperatingMode.demo, data_dir)
    mock = RuntimeStatePaths.for_mode(OperatingMode.mock, data_dir)

    assert production.config_path.name == "config.json"
    assert production.database_path.name == "base.db"
    assert production.calibration_dir == (data_dir / "calibration").resolve()
    assert development.config_path.name == "config.development.json"
    assert development.database_path.name == "base.development.db"
    assert development.calibration_dir == (data_dir / "calibration/development").resolve()
    assert demo.config_path.name == "config.demo.json"
    assert demo.database_path.name == "base.demo.db"
    assert mock.config_path.name == "config.mock.json"
    assert mock.database_path.name == "base.mock.db"


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


def test_add_mode_suffix_is_idempotent() -> None:
    path = Path("storage.development.json")

    assert add_mode_suffix(path, OperatingMode.development) == path
    assert add_mode_suffix(Path("storage.json"), OperatingMode.production) == Path("storage.json")
    assert infer_mode_from_path(path) == OperatingMode.development
    assert infer_mode_from_path(Path("storage.json")) == OperatingMode.production


def test_resolve_storage_path_suffixes_custom_nested_path(tmp_path: Path) -> None:
    paths = RuntimeStatePaths.for_mode(OperatingMode.demo, tmp_path / "data")
    config = type(
        "Config",
        (),
        {"robot": type("Robot", (), {"grid": type("Grid", (), {"path": "grids/main.json"})()})()},
    )()

    assert paths.resolve_storage_path(config) == (tmp_path / "data/grids/main.demo.json").resolve()


def test_active_config_override_uses_current_mode_for_storage(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "custom/config.json"
    monkeypatch.setenv("OPENSHELF_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSHELF_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OPERATING_MODE", "development")
    config = type(
        "Config",
        (),
        {"robot": type("Robot", (), {"grid": type("Grid", (), {"path": "storage.json"})()})()},
    )()

    paths = paths_for_config_file(config_path)

    assert paths.mode == OperatingMode.development
    assert paths.resolve_storage_path(config, config_path=config_path) == (
        tmp_path / "custom/storage.development.json"
    ).resolve()


def test_config_override_preserves_config_but_still_forks_companion_state(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    custom_dir = tmp_path / "custom"
    data_dir.mkdir()
    custom_dir.mkdir()
    custom_config = custom_dir / "config.json"
    custom_config.write_text('{"robot": {"grid": {"path": "storage.json"}}}', encoding="utf-8")
    (data_dir / "config.json").write_text('{"robot": {"grid": {"path": "storage.json"}}}', encoding="utf-8")
    (data_dir / "storage.json").write_text('{"workspace": "production"}', encoding="utf-8")
    with sqlite3.connect(data_dir / "base.db") as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('production')")
    monkeypatch.setenv("OPENSHELF_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSHELF_CONFIG_PATH", str(custom_config))
    monkeypatch.setenv("OPERATING_MODE", "development")

    paths = RuntimeStatePaths.current()
    ensure_mode_workspace(paths)

    assert custom_config.read_text(encoding="utf-8") == (
        '{"robot": {"grid": {"path": "storage.json"}}}'
    )
    assert (custom_dir / "storage.development.json").read_text(encoding="utf-8") == (
        '{"workspace": "production"}'
    )
    with sqlite3.connect(data_dir / "base.development.db") as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("production",)


def test_workspace_manifest_accepts_storage_outside_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shared_dir = tmp_path / "shared"
    data_dir.mkdir()
    shared_dir.mkdir()
    production_storage = shared_dir / "storage.json"
    production_storage.write_text('{"production": true}', encoding="utf-8")
    (data_dir / "config.json").write_text(
        json.dumps({"robot": {"grid": {"path": str(production_storage)}}}),
        encoding="utf-8",
    )

    manifest = ensure_mode_workspace(RuntimeStatePaths.for_mode(OperatingMode.demo, data_dir))

    demo_storage = shared_dir / "storage.demo.json"
    assert demo_storage.read_bytes() == production_storage.read_bytes()
    assert manifest is not None
    assert str(demo_storage) in manifest["copied"]


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


def test_adopted_mode_config_uses_its_blank_frame_destination(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    production_blank = tmp_path / "production/blank_frame.png"
    development_blank = tmp_path / "development/blank_frame.png"
    data_dir.mkdir()
    production_blank.parent.mkdir()
    development_blank.parent.mkdir()
    production_blank.write_bytes(b"production-blank")
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "robot": {
                    "io": {
                        "sizer": {
                            "detection": {"blank_frame_path": str(production_blank)},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (data_dir / "config.development.json").write_text(
        json.dumps(
            {
                "robot": {
                    "io": {
                        "sizer": {
                            "detection": {"blank_frame_path": str(development_blank)},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    manifest = ensure_mode_workspace(
        RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)
    )

    expected_destination = tmp_path / "development/blank_frame.development.png"
    wrong_destination = tmp_path / "production/blank_frame.development.png"
    assert expected_destination.read_bytes() == b"production-blank"
    assert wrong_destination.exists() is False
    assert manifest is not None
    assert str(expected_destination) in manifest["copied"]


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


def test_existing_manifest_does_not_block_config_created_after_legacy_setup(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    development = RuntimeStatePaths.for_mode(OperatingMode.development, data_dir)

    initial_manifest = ensure_mode_workspace(development)
    assert initial_manifest is not None
    assert development.config_path.exists() is False

    production_config = data_dir / "config.json"
    production_config.write_text('{"source": "production"}', encoding="utf-8")

    manifest = ensure_mode_workspace(development)

    assert development.config_path.read_bytes() == production_config.read_bytes()
    assert manifest is not None
    assert development.config_path.name in manifest["copied"]


def test_switching_from_config_override_to_default_ensures_default_config(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = tmp_path / "data"
    custom_config = tmp_path / "custom/config.json"
    data_dir.mkdir()
    custom_config.parent.mkdir()
    (data_dir / ".mode").write_text("development", encoding="utf-8")
    production_config = data_dir / "config.json"
    production_config.write_text('{"source": "production"}', encoding="utf-8")
    custom_config.write_text('{"source": "custom"}', encoding="utf-8")
    monkeypatch.setenv("OPENSHELF_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENSHELF_CONFIG_PATH", str(custom_config))

    ensure_mode_workspace()
    assert (data_dir / ".workspace.development.json").exists()

    monkeypatch.delenv("OPENSHELF_CONFIG_PATH")
    manifest = ensure_mode_workspace()

    default_config = data_dir / "config.development.json"
    assert default_config.read_bytes() == production_config.read_bytes()
    assert custom_config.read_text(encoding="utf-8") == '{"source": "custom"}'
    assert manifest is not None
    assert default_config.name in manifest["copied"]


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


def test_sqlite_backup_does_not_overwrite_a_concurrently_created_database(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "target.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('production')")
    original_link = os.link

    def create_target_before_publish(source_path: Path, target_path: Path, *args, **kwargs) -> None:
        if Path(target_path) == target:
            with sqlite3.connect(target) as connection:
                connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
                connection.execute("INSERT INTO state VALUES ('concurrent')")
        original_link(source_path, target_path, *args, **kwargs)

    monkeypatch.setattr(runtime_state.os, "link", create_target_before_publish)

    assert runtime_state._sqlite_backup(source, target) is False
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("concurrent",)
    assert list(tmp_path.glob(".target.db.*.tmp")) == []


def test_production_workspace_is_not_migrated_or_modified(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = data_dir / "config.json"
    config.write_text('{"production": true}', encoding="utf-8")

    result = ensure_mode_workspace(RuntimeStatePaths.for_mode(OperatingMode.production, data_dir))

    assert result is None
    assert config.read_text(encoding="utf-8") == '{"production": true}'
    assert list(data_dir.iterdir()) == [config]
