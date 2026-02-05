from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from perpetual_os1.common import (
	RootConfig,
	StorageSettings,
	PrinterSettings,
	ScannerSettings,
	CameraAppSettings,
	ToolCameraSettings,
	OverheadCameraSettings,
	NatsSettings,
	AuthSettings,
	ScaleAppSettings,
	RobotAppSettings,
	ConfigValidationError,
)

logger = logging.getLogger(__name__)

CONFIG_PATHS = [
	"data/config.json",
	"data/settings.json",
]


class Settings(BaseModel):
	storage: StorageSettings
	printer: PrinterSettings
	scanner: ScannerSettings
	robot: RobotAppSettings
	scale: ScaleAppSettings
	nats: NatsSettings
	camera: CameraAppSettings
	tool_camera: ToolCameraSettings
	overhead_camera: OverheadCameraSettings
	auth: AuthSettings

	@classmethod
	def from_root_config(cls, config: RootConfig) -> "Settings":
		return cls(
			storage=config.storage,
			printer=config.printer,
			scanner=config.scanner,
			robot=config.robot_app,
			scale=config.scale,
			nats=config.nats,
			camera=config.camera,
			tool_camera=config.tool_camera,
			overhead_camera=config.overhead_camera,
			auth=config.auth,
		)


_cached_config: Optional[RootConfig] = None
_cached_settings: Optional[Settings] = None
_cached_config_path: Optional[Path] = None


def _find_config_path() -> Optional[Path]:
	for path_str in CONFIG_PATHS:
		path = Path(path_str)
		if path.exists() is True:
			return path
	return None


def _load_legacy_settings(path: Path) -> dict:
	with open(path, "r") as f:
		return json.load(f)


def _build_minimal_config(data: dict) -> dict:
	legacy_robot = data.pop("robot", None)

	if legacy_robot is not None and "enabled" in legacy_robot:
		if "robot_app" not in data:
			data["robot_app"] = {}
		data["robot_app"]["enabled"] = legacy_robot.get("enabled", True)

	# These are minimal defaults for testing only.
	# Production deployments MUST provide data/config.json with real calibrated values.
	# The application will fail to start if config.json is missing in production.
	return {
		"name": "perpetual_os1",
		"version": "0.1.0",
		"description": "Perpetual OS1 configuration",
		"robot_app": {"enabled": False},
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
				"tof_camera": {
					"csi_port": 0,
					"resolution": [240, 180],
					"fps": 30,
					"max_distance_mm": 2000,
					"confidence_min": 0.9,
				},
			},
		},
		**data,
	}


def _apply_env_overrides(data: dict) -> dict:
	if "auth" not in data:
		data["auth"] = {}

	env = os.getenv("ENV", "").lower()

	if env in ["dev", "development"]:
		data["auth"]["dev_mode"] = True
		data["auth"]["require_auth"] = False
		data["auth"]["dev_auto_user"] = "admin@opshelf.com"
	elif env == "test":
		data["auth"]["dev_mode"] = False
		data["auth"]["require_auth"] = True
		data["auth"]["enabled"] = True

	jwt_secret = os.getenv("JWT_SECRET")
	if jwt_secret is not None:
		data["auth"]["jwt_secret"] = jwt_secret
	elif data["auth"].get("jwt_secret") is None:
		data["auth"]["jwt_secret"] = secrets.token_urlsafe(32)

	nats_domain = os.getenv("NATS_DOMAIN")
	if nats_domain is not None:
		if "nats" not in data:
			data["nats"] = {}
		data["nats"]["domain"] = nats_domain

	nats_port = os.getenv("NATS_PORT", "")
	if nats_port:
		if "nats" not in data:
			data["nats"] = {}
		data["nats"]["port"] = int(nats_port)

	return data


def _migrate_legacy_settings(settings_path: Path, config_path: Path) -> None:
	logger.info("Migrating legacy settings.json to config.json")

	legacy_data = _load_legacy_settings(settings_path)
	merged_data = _build_minimal_config(legacy_data)

	with open(config_path, "w") as f:
		json.dump(merged_data, f, indent="\t")

	logger.info("Migration complete: %s created from %s", config_path, settings_path)
	logger.warning(
		"Legacy settings.json retained for backup. "
		"Remove manually after verifying config.json works correctly."
	)


def get_root_config() -> RootConfig:
	global _cached_config, _cached_config_path

	if _cached_config is not None:
		return _cached_config

	config_path = Path("data/config.json")
	settings_path = Path("data/settings.json")

	if config_path.exists() is False and settings_path.exists() is True:
		_migrate_legacy_settings(settings_path, config_path)

	found_path = _find_config_path()

	if found_path is None:
		logger.warning("No config file found in %s, using defaults", CONFIG_PATHS)
		data = _build_minimal_config({})
		data = _apply_env_overrides(data)
		from perpetual_os1.common import load_and_validate_config
		_cached_config = load_and_validate_config(data)
		_cached_config_path = Path("data/config.json")
		return _cached_config

	logger.info("Loading config from %s", found_path)
	_cached_config_path = found_path

	if found_path.name == "settings.json":
		logger.warning(
			"Loading from legacy settings.json - config.json migration may have failed"
		)
		legacy_data = _load_legacy_settings(found_path)
		data = _build_minimal_config(legacy_data)
	else:
		with open(found_path, "r") as f:
			data = json.load(f)

	data = _apply_env_overrides(data)

	from perpetual_os1.common import load_and_validate_config

	try:
		_cached_config = load_and_validate_config(data, str(found_path))
	except ConfigValidationError as e:
		logger.error("Config validation failed: %s", e)
		raise

	return _cached_config


def get_settings() -> Settings:
	global _cached_settings

	if _cached_settings is not None:
		return _cached_settings

	config = get_root_config()
	_cached_settings = Settings.from_root_config(config)
	return _cached_settings


def clear_config_cache() -> None:
	global _cached_config, _cached_settings
	_cached_config = None
	_cached_settings = None


def get_config_path() -> Optional[Path]:
	return _cached_config_path


def _create_config_backup(config_path: Path) -> Optional[Path]:
	if config_path.exists() is False:
		return None

	timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	backup_name = f"{config_path.stem}_{timestamp}{config_path.suffix}"
	backup_path = config_path.parent / backup_name

	shutil.copy2(config_path, backup_path)
	logger.info("Config backup created: %s", backup_path)

	return backup_path


def save_root_config(config: RootConfig) -> None:
	global _cached_config, _cached_settings, _cached_config_path

	if _cached_config_path is None:
		_cached_config_path = Path("data/config.json")

	_cached_config_path.parent.mkdir(parents=True, exist_ok=True)

	_create_config_backup(_cached_config_path)

	data = config.model_dump(mode="json")

	with open(_cached_config_path, "w") as f:
		json.dump(data, f, indent="\t")

	_cached_config = config
	_cached_settings = None

	logger.info("Config saved to %s", _cached_config_path)
