import json

from openshelf_os.common import RootConfig
from openshelf_os_server import settings as settings_module


MINIMAL_CONFIG = {
	"name": "test",
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
				"Z_MOVE_RPM": 1,
				"Z_MOVE_ACCEL": 1,
				"ROTATION_FRONT": 0,
				"ROTATION_BACK": 0,
			},
			"motors": [],
			"poe_camera": {
				"url": "",
			},
		},
		"grid": {"path": "storage.json"},
		"io": {
			"motors": [],
		},
	},
	"auth": {"require_auth": False},
}


def test_save_root_config_creates_backup_in_data_config_dir(tmp_path, monkeypatch):
	config_path = tmp_path / "data" / "config.json"
	config_path.parent.mkdir(parents=True)
	previous_payload = {"legacy": True}
	config_path.write_text(json.dumps(previous_payload), encoding="utf-8")

	monkeypatch.setattr(settings_module, "_cached_config_path", config_path)
	monkeypatch.setattr(settings_module, "_cached_config", None)
	monkeypatch.setattr(settings_module, "_cached_settings", None)

	config = RootConfig.model_validate(MINIMAL_CONFIG)
	settings_module.save_root_config(config)

	backups = list((tmp_path / "data" / "config").glob("*.json"))

	assert len(backups) == 1
	assert backups[0].name.endswith(".production.json")
	assert json.loads(backups[0].read_text(encoding="utf-8")) == previous_payload
	assert json.loads(config_path.read_text(encoding="utf-8"))["name"] == "test"


def test_save_root_config_tags_development_backup(tmp_path, monkeypatch):
	config_path = tmp_path / "data" / "config.development.json"
	config_path.parent.mkdir(parents=True)
	config_path.write_text(json.dumps({"legacy": True}), encoding="utf-8")

	monkeypatch.setattr(settings_module, "_cached_config_path", config_path)
	monkeypatch.setattr(settings_module, "_cached_config", None)
	monkeypatch.setattr(settings_module, "_cached_settings", None)

	settings_module.save_root_config(RootConfig.model_validate(MINIMAL_CONFIG), version_id="version-1")

	backup = tmp_path / "data" / "config" / "version-1.development.json"
	assert json.loads(backup.read_text(encoding="utf-8")) == {"legacy": True}


def test_save_root_config_tags_active_mode_for_explicit_config_override(tmp_path, monkeypatch):
	data_dir = tmp_path / "data"
	config_path = tmp_path / "custom" / "config.json"
	config_path.parent.mkdir(parents=True)
	config_path.write_text(json.dumps({"legacy": True}), encoding="utf-8")
	monkeypatch.setenv("OPENSHELF_DATA_DIR", str(data_dir))
	monkeypatch.setenv("OPENSHELF_CONFIG_PATH", str(config_path))
	monkeypatch.setenv("OPERATING_MODE", "demo")
	monkeypatch.setattr(settings_module, "_cached_config_path", config_path)
	monkeypatch.setattr(settings_module, "_cached_config", None)
	monkeypatch.setattr(settings_module, "_cached_settings", None)

	settings_module.save_root_config(RootConfig.model_validate(MINIMAL_CONFIG), version_id="version-1")

	backup = config_path.parent / "config" / "version-1.demo.json"
	assert json.loads(backup.read_text(encoding="utf-8")) == {"legacy": True}
