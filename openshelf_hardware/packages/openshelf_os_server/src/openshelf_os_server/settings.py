from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from openshelf_os.config.schema import CURRENT_SCHEMA_VERSION

from openshelf_os.common import (
    RootConfig,
    StorageSettings,
    PrinterSettings,
    ScannerSettings,
    ToolCameraSettings,
    OverheadCameraSettings,
    OverviewCameraSettings,
    AuthSettings,
    ScaleAppSettings,
    RobotAppSettings,
    ConfigValidationError,
    CONFIG_PATH,
    INSECURE_PUBLISHED_SECRETS,
    OperatingMode,
    write_json_file_with_backup,
    load_and_validate_config,
)
from openshelf_os.operating_mode import get_operating_mode as get_shared_operating_mode, has_production_traits
from openshelf_os.runtime_state import DATA_DIR_ENV, paths_for_config_file
from openshelf_os.serial_ports import (
    DEFAULT_LASER_PORT,
    DEFAULT_MOTOR_PORT,
    DEFAULT_SCALE_PORT,
)

logger = logging.getLogger(__name__)

MODE_FILE = Path(os.getenv(DATA_DIR_ENV, "data")) / ".mode"
LEGACY_SETTINGS_PATH = Path(os.getenv(DATA_DIR_ENV, "data")) / "settings.json"

# The dev-mode fallback identity. It must name a user that every build creates,
# otherwise unauthenticated requests answer 503 "Dev auto-user not found in
# database". main._ensure_developer_user recreates this one on every startup.
DEVELOPER_SYSTEM_USER_EMAIL = "developer@opshelf.com"


def _config_paths() -> list[Path]:
    paths = [CONFIG_PATH]
    if get_operating_mode() == OperatingMode.production:
        paths.append(LEGACY_SETTINGS_PATH)
    return paths


class Settings(BaseModel):
    storage: StorageSettings
    printer: PrinterSettings
    scanner: ScannerSettings
    robot: RobotAppSettings
    scale: ScaleAppSettings
    tool_camera: ToolCameraSettings
    overhead_camera: OverheadCameraSettings
    overview_one_camera: OverviewCameraSettings
    overview_two_camera: OverviewCameraSettings
    auth: AuthSettings

    @classmethod
    def from_root_config(cls, config: RootConfig) -> "Settings":
        return cls(
            storage=config.storage,
            printer=config.printer,
            scanner=config.scanner,
            robot=config.robot_app,
            scale=config.scale,
            tool_camera=config.tool_camera,
            overhead_camera=config.overhead_camera,
            overview_one_camera=config.overview_one_camera,
            overview_two_camera=config.overview_two_camera,
            auth=config.auth,
        )


_cached_config: Optional[RootConfig] = None
_cached_settings: Optional[Settings] = None
_cached_config_path: Optional[Path] = None


def get_operating_mode() -> OperatingMode:
    return get_shared_operating_mode(MODE_FILE)


def _find_config_path() -> Optional[Path]:
    for path in _config_paths():
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
        "schema_version": CURRENT_SCHEMA_VERSION,
        "name": "openshelf_os",
        "version": "0.1.0",
        "description": "OpenShelf OS configuration",
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
                },
                "motors": [],
                "poe_camera": {
                    "url": "",
                },
                "laser": {
                    "enabled": False,
                    "device_id": 1,
                    "read_function_code": 3,
                    "register_address": 0,
                    "register_count": 2,
                    "distance_register_index": 1,
                    "distance_scale_mm": 0.001,
                },
                },
                "grid": {
                    "path": "storage.json",
                    "scan": {
                        "laser_offset_x_mm": None,
                        "laser_offset_y_mm": None,
                        "fine_scan_step_mm": 1.0,
                        "front_bounds_x_min_mm": 0.0,
                        "front_bounds_x_max_mm": 950.0,
                        "front_bounds_y_min_mm": 0.0,
                        "front_bounds_y_max_mm": 1250.0,
                        "back_bounds_x_min_mm": 0.0,
                        "back_bounds_x_max_mm": 950.0,
                        "back_bounds_y_min_mm": 0.0,
                        "back_bounds_y_max_mm": 1250.0,
                        "angle_calibration_x_max_mm": 950.0,
                    },
                },
            "io": {
                "motors": [],
            },
            "bus": {
                "motor": {
                    "port": DEFAULT_MOTOR_PORT,
                    "baudrate": 115200,
                    "parity": "N",
                    "timeout": 2.0,
                    "retry_delay_s": 0.25,
                },
                "scale": {
                    "port": DEFAULT_SCALE_PORT,
                    "baudrate": 9600,
                    "parity": "N",
                    "timeout": 2.0,
                    "retry_delay_s": 0.25,
                },
                "laser": {
                    "port": DEFAULT_LASER_PORT,
                    "baudrate": 115200,
                    "parity": "N",
                    "timeout": 2.0,
                    "retry_delay_s": 0.25,
                },
            },
        },
        **data,
    }


def _apply_env_overrides(data: dict) -> dict:
    if "auth" not in data:
        data["auth"] = {}

    env = os.getenv("ENV", "").lower()

    if env != "test":
        mode = get_operating_mode()
        if has_production_traits(mode) is True:
            env = "production"
        else:
            env = "dev"

    if env in ["dev", "development"]:
        data["auth"]["dev_mode"] = True
        data["auth"]["require_auth"] = False
        data["auth"]["dev_auto_user"] = DEVELOPER_SYSTEM_USER_EMAIL
    elif env == "test":
        data["auth"]["dev_mode"] = False
        data["auth"]["require_auth"] = True
        data["auth"]["enabled"] = True

    # A blank JWT_SECRET (e.g. an unset compose passthrough) must not override a
    # usable secret, otherwise tokens would be signed with an empty key. The literal
    # docker-compose.yml used to publish is rejected as well: anyone who can read the
    # repository could forge a session with it, and it leaked into config.json on
    # devices that saved their config while it was in effect.
    jwt_secret = os.getenv("JWT_SECRET") or data["auth"].get("jwt_secret")
    if not jwt_secret or jwt_secret in INSECURE_PUBLISHED_SECRETS:
        if jwt_secret:
            logger.warning(
                "JWT_SECRET is a published default, using a generated secret instead; "
                "existing tokens stop verifying"
            )
        jwt_secret = secrets.token_urlsafe(32)
    data["auth"]["jwt_secret"] = jwt_secret

    return data


def _migrate_legacy_settings(settings_path: Path, config_path: Path) -> None:
    logger.info("Migrating legacy settings.json to config.json")

    legacy_data = _load_legacy_settings(settings_path)
    merged_data = _build_minimal_config(legacy_data)

    write_json_file_with_backup(
        config_path,
        merged_data,
        indent="\t",
        backup_group_name="config",
    )

    logger.info("Migration complete: %s created from %s", config_path, settings_path)
    logger.warning(
        "Legacy settings.json retained for backup. "
        "Remove manually after verifying config.json works correctly."
    )


def get_root_config() -> RootConfig:
    global _cached_config, _cached_config_path

    if _cached_config is not None:
        return _cached_config

    config_path = CONFIG_PATH
    settings_path = LEGACY_SETTINGS_PATH

    if get_operating_mode() == OperatingMode.production and config_path.exists() is False and settings_path.exists() is True:
        _migrate_legacy_settings(settings_path, config_path)

    found_path = _find_config_path()

    if found_path is None:
        logger.warning("No config file found in %s, using defaults", _config_paths())
        data = _build_minimal_config({})
        data = _apply_env_overrides(data)
        _cached_config = load_and_validate_config(data)
        _cached_config_path = CONFIG_PATH
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


def save_root_config(config: RootConfig, *, version_id: str | None = None) -> None:
    global _cached_config, _cached_settings, _cached_config_path

    if _cached_config_path is None:
        _cached_config_path = CONFIG_PATH

    _cached_config_path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json")
    write_json_file_with_backup(
        _cached_config_path,
        data,
        indent="\t",
        backup_group_name="config",
        backup_mode=paths_for_config_file(_cached_config_path).mode.value,
        version_id=version_id,
    )

    _cached_config = config
    _cached_settings = None

    logger.info("Config saved to %s", _cached_config_path)
