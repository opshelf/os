"""Server on-startup auto-migration of the active config file, via settings.get_root_config()."""

from __future__ import annotations

import json

import pytest

from openshelf_os.config.exceptions import ConfigError
from openshelf_os_server import settings


@pytest.fixture
def v4_config():
    return {
        "schema_version": 4,
        "name": "auto-migrate-test",
        "version": "0.1.0",
        "robot": {
            "tool": {
                "parameters": {
                    "IDLE_POSITION": [0, 0],
                    "PICKUP_POSITION_L": [0, 0],
                    "PICKUP_POSITION_H": [0, 0],
                    "SCAN_POSITION": [0, 0],
                    "STAGE_EXTENSION_IO_BOX": 0,
                    "STAGE_NUDGE": 0,
                    "STAGE_EXTENSION_SHELF_FRONT": 0,
                    "STAGE_EXTENSION_SHELF_BACK": 0,
                    "Z_EXTENSION_PICK": 0,
                    "Z_EXTENSION_PLACE": 0,
                    "Z_EXTENSION_IO": 0,
                    "Z_MOVE_RPM": 2,
                    "Z_MOVE_ACCEL": 2,
                    "ROTATION_FRONT": 0,
                    "ROTATION_BACK": 0,
                },
                "motors": [],
                "poe_camera": {"url": ""},
            },
            "io": {"motors": []},
            "grid": {
                "path": "storage.json",
                "scan": {
                    "left_shelf_scout_x_mm": 120.0,
                    "right_shelf_scout_x_mm": 45.0,
                },
            },
        },
        "storage": {
            "bins": [
                {
                    "barcode": "bin-1",
                    "home_location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 1},
                    "inner_dimensions": {"width": 100.0, "height": 100.0, "length": 100.0},
                    "max_items": 4,
                }
            ]
        },
    }


@pytest.fixture
def config_file(tmp_path, v4_config, monkeypatch):
    """Point the settings module at a tmp config file in mock mode; return (path, raw bytes)."""
    path = tmp_path / "config.json"
    raw = json.dumps(v4_config).encode()
    path.write_bytes(raw)
    mode_file = tmp_path / ".mode"
    mode_file.write_text("mock")
    monkeypatch.setattr(settings, "CONFIG_PATH", path)
    monkeypatch.setattr(settings, "MODE_FILE", mode_file)
    settings.clear_config_cache()
    return path, raw


def test_get_root_config_auto_migrates_v4_and_backs_up(config_file):
    path, raw = config_file
    config = settings.get_root_config()
    assert config.schema_version == 6
    assert path.with_name("config.json.schema-v4.bak").read_bytes() == raw
    on_disk = json.loads(path.read_bytes())
    assert on_disk["schema_version"] == 6
    assert on_disk["robot"]["tool"]["parameters"]["PICKUP_POSITION_MULTI_ITEM"] == [0, 0]
    assert "front_shelf_scout_x_mm" in on_disk["robot"]["grid"]["scan"]
    assert on_disk["storage"]["bins"][0]["home_location"]["side"] == "front_module"


def test_existing_backup_confails_startup_and_leaves_file_untouched(config_file, monkeypatch):
    path, raw = config_file
    path.with_name("config.json.schema-v4.bak").write_bytes(b"pre-existing rollback")
    with pytest.raises(ConfigError, match="migration backup already exists"):
        settings.get_root_config()
    assert path.read_bytes() == raw


def test_future_version_fails_startup_without_touching_file(config_file, monkeypatch):
    path, raw = config_file
    future = json.loads(raw)
    future["schema_version"] = 7
    path.write_bytes(json.dumps(future).encode())
    with pytest.raises(ConfigError, match="newer than supported"):
        settings.get_root_config()
    assert json.loads(path.read_bytes())["schema_version"] == 7
    assert not path.with_name("config.json.schema-v7.bak").exists()


def test_failed_validation_fails_startup_and_leaves_file_untouched(config_file):
    path, raw = config_file
    broken = json.loads(raw)
    broken["robot"]["tool"]["parameters"]["IDLE_POSITION"] = "not-a-list"
    broken_raw = json.dumps(broken).encode()
    path.write_bytes(broken_raw)
    with pytest.raises(ConfigError, match="failed validation"):
        settings.get_root_config()
    assert path.read_bytes() == broken_raw
    assert list(path.parent.glob("config.json.*.bak")) == []
