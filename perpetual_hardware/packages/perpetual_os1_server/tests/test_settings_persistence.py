import json

from perpetual_os1.common import RootConfig
from perpetual_os1_server import settings as settings_module


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
				"JOG_X": 0,
				"JOG_Y": 0,
				"PXMM": 1,
				"PYMM": 1,
			},
			"motors": [],
			"poe_camera": {
				"ip": "127.0.0.1",
				"port": 80,
				"url": "",
				"H_ref_size": [1920, 1080],
				"pattern_size": [9, 6],
				"square_size_mm": 25.0,
			},
		},
		"grid": {"path": "storage.json"},
		"io": {
			"motors": [],
			"usb_camera": {
				"usb_index": 0,
				"usb_fourcc": "MJPG",
				"usb_exposure": 25,
				"square_mm": 7.5,
				"fx_px": 708,
				"fy_px": 708,
				"px_per_mm": 1.2,
				"confidence_min": 0,
				"headless_fallback_path": "./fallback.jpg",
			},
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
	assert json.loads(backups[0].read_text(encoding="utf-8")) == previous_payload
	assert json.loads(config_path.read_text(encoding="utf-8"))["name"] == "test"
