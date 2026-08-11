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
