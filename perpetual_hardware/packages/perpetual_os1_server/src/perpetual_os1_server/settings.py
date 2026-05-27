from __future__ import annotations

import json
import logging
import os
import secrets
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
    AuthSettings,
    ScaleAppSettings,
    RobotAppSettings,
    ConfigValidationError,
    OperatingMode,
    write_json_file_with_backup,
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
            camera=config.camera,
            tool_camera=config.tool_camera,
            overhead_camera=config.overhead_camera,
            auth=config.auth,
        )


MODE_FILE = Path("data/.mode")

_cached_config: Optional[RootConfig] = None
_cached_settings: Optional[Settings] = None
_cached_config_path: Optional[Path] = None
_cached_operating_mode: Optional[OperatingMode] = None


def get_operating_mode() -> OperatingMode:
    global _cached_operating_mode

    if _cached_operating_mode is not None:
        return _cached_operating_mode

    mode_map = {
        "demo": OperatingMode.demo,
        "development": OperatingMode.development,
        "production": OperatingMode.production,
    }

    if MODE_FILE.exists() is True:
        raw = MODE_FILE.read_text(encoding="utf-8").strip().lower()
        if raw in mode_map:
            _cached_operating_mode = mode_map[raw]
            return _cached_operating_mode

    raw = os.getenv("OPERATING_MODE", "").strip().lower()
    _cached_operating_mode = mode_map.get(raw, OperatingMode.development)
    return _cached_operating_mode


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
                        "wall_depth_tolerance_mm": 2.0,
                        "expected_hole_width_mm": 26.0,
                        "expected_hole_height_mm": 30.0,
                        "hole_width_tolerance_mm": 10.0,
                        "hole_height_tolerance_mm": 10.0,
                        "min_vertical_wall_coverage_mm": 10.0,
                        "min_horizontal_wall_coverage_mm": 30.0,
                        "laser_offset_x_mm": None,
                        "laser_offset_y_mm": None,
                        "fine_scan_step_mm": 2.0,
                        "laser_sample_settle_ms": 50,
                        "laser_samples_per_point": 3,
                        "left_bounds_x_min_mm": 0.0,
                        "left_bounds_x_max_mm": 950.0,
                        "left_bounds_y_min_mm": 0.0,
                        "left_bounds_y_max_mm": 1250.0,
                        "right_bounds_x_min_mm": 0.0,
                        "right_bounds_x_max_mm": 950.0,
                        "right_bounds_y_min_mm": 0.0,
                        "right_bounds_y_max_mm": 1250.0,
                        "angle_calibration_x_min_mm": 0.0,
                        "angle_calibration_x_max_mm": 950.0,
                        "angle_calibration_x_step_mm": 100.0,
                        "angle_calibration_x_count": 5,
                        "angle_calibration_z_offset_min_mm": -8.0,
                        "angle_calibration_z_offset_max_mm": 8.0,
                        "angle_calibration_z_offset_step_mm": 4.0,
                        "angle_calibration_z_offset_count": 1,
                        "angle_calibration_rotation_min_deg": -8.0,
                        "angle_calibration_rotation_max_deg": 8.0,
                        "angle_calibration_rotation_step_deg": 1.0,
                        "angle_calibration_beam_zero_deg": 0.0,
                        "angle_calibration_alpha_min_deg": -10.0,
                        "angle_calibration_alpha_max_deg": 10.0,
                        "angle_calibration_coarse_step_deg": 0.1,
                        "angle_calibration_fine_window_deg": 0.3,
                        "angle_calibration_fine_step_deg": 0.005,
                        "angle_calibration_stage_stall_rotation_min_deg": 0.0,
                        "angle_calibration_stage_stall_rotation_max_deg": 5.5,
                        "angle_calibration_stage_stall_coarse_min_deg": 0.0,
                        "angle_calibration_stage_stall_coarse_max_deg": 5.5,
                        "angle_calibration_stage_stall_coarse_step_deg": 1.0,
                        "angle_calibration_stage_stall_coarse_start_deg": 4.5,
                        "angle_calibration_stage_stall_fine_window_deg": 1.5,
                        "angle_calibration_stage_stall_fine_step_deg": 0.25,
                        "angle_calibration_stage_stall_fast_approach_mm": 0.0,
                        "angle_calibration_stage_stall_validation_count": 3,
                        "angle_calibration_stage_stall_validation_repeats": 3,
                        "angle_calibration_stage_stall_validation_retry_count": 1,
                        "angle_calibration_stage_stall_coarse_retry_count": 1,
                        "angle_calibration_stage_stall_min_valid_stage_position_mm": 40.0,
                        "angle_calibration_stage_stall_validation_plateau_tolerance_mm": 0.75,
                        "angle_calibration_stage_stall_coarse_validation_count": 1,
                        "angle_calibration_stage_stall_validation_high_side_window_deg": 0.75,
                        "angle_calibration_stage_stall_low_contact_retry_count": 1,
                        "angle_calibration_stage_stall_validation_min_valid_sample_count": 2,
                    },
                },
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
            "bus": {
                "motor": {
                    "ports": ["/dev/ttyUSB1"],
                    "baudrate": 115200,
                    "parity": "N",
                    "timeout": 2.0,
                    "retry_delay_s": 0.25,
                },
                "scale": {
                    "ports": ["/dev/ttyUSB0"],
                    "baudrate": 9600,
                    "parity": "N",
                    "timeout": 2.0,
                    "retry_delay_s": 0.25,
                },
                "laser": {
                    "ports": ["/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B001K430-if00-port0"],
                    "baudrate": 9600,
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

    if env == "":
        mode = get_operating_mode()
        if mode == OperatingMode.production:
            env = "production"
        else:
            env = "dev"

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
    global _cached_config, _cached_settings, _cached_operating_mode
    _cached_config = None
    _cached_settings = None
    _cached_operating_mode = None


def get_config_path() -> Optional[Path]:
    return _cached_config_path


def save_root_config(config: RootConfig) -> None:
    global _cached_config, _cached_settings, _cached_config_path

    if _cached_config_path is None:
        _cached_config_path = Path("data/config.json")

    _cached_config_path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json")
    write_json_file_with_backup(
        _cached_config_path,
        data,
        indent="\t",
        backup_group_name="config",
    )

    _cached_config = config
    _cached_settings = None

    logger.info("Config saved to %s", _cached_config_path)
