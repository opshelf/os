from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from openshelf_os_server import launcher
from openshelf_os_server.settings import _build_minimal_config


def test_launcher_migrates_legacy_settings_before_development_fork(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".mode").write_text("development", encoding="utf-8")
    (data_dir / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENSHELF_DATA_DIR", str(data_dir))

    launcher._migrate_legacy_production_config()
    from openshelf_os.runtime_state import ensure_mode_workspace

    ensure_mode_workspace()

    assert (data_dir / "config.json").exists()
    assert (data_dir / "config.development.json").read_bytes() == (data_dir / "config.json").read_bytes()


def test_bootstrap_selects_and_clones_development_runtime_files(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".mode").write_text("development", encoding="utf-8")
    (data_dir / "config.json").write_text(
        json.dumps(_build_minimal_config({})),
        encoding="utf-8",
    )
    (data_dir / "storage.json").write_text('{"left_module": [], "right_module": []}', encoding="utf-8")
    with sqlite3.connect(data_dir / "base.db") as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('production')")

    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment["OPENSHELF_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sqlite3
from openshelf_os.runtime_state import ensure_mode_workspace
ensure_mode_workspace()
from openshelf_os.common import CONFIG_PATH, load_config_file, resolve_grid_path
from openshelf_os_server.database.base import DB_PATH
config = load_config_file(CONFIG_PATH)
with sqlite3.connect(DB_PATH) as connection:
    value = connection.execute('SELECT value FROM state').fetchone()[0]
print(json.dumps({
    'config': CONFIG_PATH.name,
    'database': DB_PATH.name,
    'storage': __import__('pathlib').Path(resolve_grid_path(config)).name,
    'value': value,
}))
""",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "config": "config.development.json",
        "database": "base.development.db",
        "storage": "storage.development.json",
        "value": "production",
    }


SERVER_ENTRY_SCRIPT = """
import pathlib
import sys
import uvicorn

marker = pathlib.Path(sys.argv[1])
mode_config = pathlib.Path(sys.argv[2])


def record_serve(*args, **kwargs):
    marker.write_text('forked' if mode_config.exists() else 'missing', encoding='utf-8')


uvicorn.run = record_serve

from openshelf_os_server.main import start

start()
"""


def _write_production_workspace(data_dir: Path, mode: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / ".mode").write_text(mode, encoding="utf-8")
    (data_dir / "config.json").write_text(
        json.dumps(_build_minimal_config({"description": "production-calibration"})),
        encoding="utf-8",
    )
    (data_dir / "storage.json").write_text('{"left_module": [], "right_module": []}', encoding="utf-8")
    with sqlite3.connect(data_dir / "base.db") as connection:
        connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO state VALUES ('production')")


def _run_server_entry(data_dir: Path, marker: Path, mode_config: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("DATABASE_URL", None)
    environment.pop("OPENSHELF_CONFIG_PATH", None)
    environment["OPENSHELF_DATA_DIR"] = str(data_dir)
    return subprocess.run(
        [sys.executable, "-c", SERVER_ENTRY_SCRIPT, str(marker), str(mode_config)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_server_entry_forks_production_state_before_serving(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_production_workspace(data_dir, "development")
    production_config_bytes = (data_dir / "config.json").read_bytes()
    production_storage_bytes = (data_dir / "storage.json").read_bytes()
    marker = tmp_path / "serve_marker"
    development_config = data_dir / "config.development.json"

    _run_server_entry(data_dir, marker, development_config)

    assert marker.read_text(encoding="utf-8") == "forked"
    assert development_config.read_bytes() == production_config_bytes
    assert (data_dir / "storage.development.json").read_bytes() == production_storage_bytes
    with sqlite3.connect(data_dir / "base.development.db") as connection:
        assert connection.execute("SELECT value FROM state").fetchone() == ("production",)
    assert (data_dir / "config.json").read_bytes() == production_config_bytes
    assert (data_dir / "storage.json").read_bytes() == production_storage_bytes


def test_server_entry_keeps_existing_mode_state(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_production_workspace(data_dir, "development")
    development_config = data_dir / "config.development.json"
    development_config.write_text(
        json.dumps(_build_minimal_config({"description": "development-calibration"})),
        encoding="utf-8",
    )
    development_config_bytes = development_config.read_bytes()
    development_storage = data_dir / "storage.development.json"
    development_storage.write_text('{"left_module": [], "right_module": [1]}', encoding="utf-8")
    marker = tmp_path / "serve_marker"

    _run_server_entry(data_dir, marker, development_config)

    assert marker.read_text(encoding="utf-8") == "forked"
    assert development_config.read_bytes() == development_config_bytes
    assert development_storage.read_text(encoding="utf-8") == '{"left_module": [], "right_module": [1]}'


def test_server_entry_without_production_state_keeps_defaults(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".mode").write_text("development", encoding="utf-8")
    marker = tmp_path / "serve_marker"
    development_config = data_dir / "config.development.json"

    _run_server_entry(data_dir, marker, development_config)

    assert marker.read_text(encoding="utf-8") == "missing"
    assert development_config.exists() is False
    assert (data_dir / "config.json").exists() is False
    assert (data_dir / "storage.development.json").exists() is False


def test_bootstrap_runtime_state_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    _write_production_workspace(data_dir, "development")
    monkeypatch.setenv("OPENSHELF_DATA_DIR", str(data_dir))
    monkeypatch.delenv("OPENSHELF_CONFIG_PATH", raising=False)

    launcher.bootstrap_runtime_state()
    development_config = data_dir / "config.development.json"
    development_config.write_text('{"source": "development"}', encoding="utf-8")
    launcher.bootstrap_runtime_state()

    assert development_config.read_text(encoding="utf-8") == '{"source": "development"}'
