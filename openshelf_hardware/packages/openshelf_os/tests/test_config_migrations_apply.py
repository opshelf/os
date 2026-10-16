"""File-level behavior of the shared config-migration pipeline (CLI + server auto-migration)."""

from __future__ import annotations

import copy
import json

import pytest

from openshelf_os.config_migrations import apply


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


def _write(tmp_path, name: str, config: dict) -> tuple:
    path = tmp_path / name
    raw = json.dumps(config).encode()
    path.write_bytes(raw)
    return path, raw


def test_migrates_in_place_with_backup_then_noop(tmp_path, v4_config):
    path, raw = _write(tmp_path, "config.json", v4_config)
    changed, changes, backup = apply.migrate_config_file(path)
    assert changed is True
    assert len(changes) > 1 and changes[-1] == "schema_version: 5 -> 6"
    assert any("front_shelf_scout_x_mm" in c for c in changes)
    assert backup == path.with_name("config.json.schema-v4.bak")
    assert backup.read_bytes() == raw
    migrated = json.loads(path.read_bytes())
    assert migrated["schema_version"] == 6
    assert "front_shelf_scout_x_mm" in migrated["robot"]["grid"]["scan"]
    assert migrated["storage"]["bins"][0]["home_location"]["side"] == "front_module"

    again_changed, again_changes, again_backup = apply.migrate_config_file(path)
    assert again_changed is False and again_changes == [] and again_backup is None
    assert path.read_bytes() != raw  # still the migrated content
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json", "config.json.schema-v4.bak"]


def test_backup_conflict_refuses_without_touching_file(tmp_path, v4_config):
    path, raw = _write(tmp_path, "config.json", v4_config)
    path.with_name("config.json.schema-v4.bak").write_bytes(b"pre-existing rollback")
    with pytest.raises(ValueError, match="migration backup already exists"):
        apply.migrate_config_file(path)
    assert path.read_bytes() == raw
    assert path.with_name("config.json.schema-v4.bak").read_bytes() == b"pre-existing rollback"


def test_failed_validation_leaves_file_untouched(tmp_path, v4_config):
    broken = copy.deepcopy(v4_config)
    broken["robot"]["tool"]["parameters"]["IDLE_POSITION"] = "not-a-list"
    path, raw = _write(tmp_path, "config.json", broken)
    with pytest.raises(ValueError, match="failed validation"):
        apply.migrate_config_file(path)
    assert path.read_bytes() == raw
    assert list(tmp_path.iterdir()) == [path]


def test_failed_temp_write_leaves_no_backup_and_retries_cleanly(tmp_path, v4_config, monkeypatch):
    path, raw = _write(tmp_path, "config.json", v4_config)

    def broken_tempfile(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(apply.tempfile, "NamedTemporaryFile", broken_tempfile)
    with pytest.raises(OSError):
        apply.migrate_config_file(path)
    monkeypatch.undo()
    assert path.read_bytes() == raw
    assert not path.with_name("config.json.schema-v4.bak").exists()
    assert list(tmp_path.iterdir()) == [path]

    changed, changes, backup = apply.migrate_config_file(path)
    assert changed is True
    assert backup == path.with_name("config.json.schema-v4.bak")
    assert backup.read_bytes() == raw
    assert json.loads(path.read_bytes())["schema_version"] == 6


def test_failed_backup_write_leaves_no_backup_and_retries_cleanly(tmp_path, v4_config, monkeypatch):
    path, raw = _write(tmp_path, "config.json", v4_config)

    fsync_calls = 0

    def flaky_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:  # the backup write's fsync
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(apply.os, "fsync", flaky_fsync)
    with pytest.raises(OSError):
        apply.migrate_config_file(path)
    monkeypatch.undo()
    assert path.read_bytes() == raw
    assert not path.with_name("config.json.schema-v4.bak").exists()
    assert list(tmp_path.iterdir()) == [path]

    changed, changes, backup = apply.migrate_config_file(path)
    assert changed is True
    assert backup == path.with_name("config.json.schema-v4.bak")
    assert backup.read_bytes() == raw
    assert json.loads(path.read_bytes())["schema_version"] == 6
