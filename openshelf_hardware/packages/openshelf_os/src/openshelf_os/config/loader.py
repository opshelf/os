from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import ValidationError

from openshelf_os.config.exceptions import ConfigError, ConfigMissingError, ConfigValidationError
from openshelf_os.config.root import RootConfig


CONFIG_PATH = Path("data/config.json")


def load_and_validate_config(cfg: Union[Dict[str, Any], str], source_path: Optional[str] = None) -> RootConfig:
    data: Dict[str, Any]

    if isinstance(cfg, str):
        data = json.loads(cfg)
    else:
        data = cfg

    try:
        return RootConfig.model_validate(data)
    except ValidationError as e:
        errors = [dict(err) for err in e.errors()]
        raise ConfigValidationError(errors, source_path) from e


def load_config_file(path: Union[str, Any]) -> RootConfig:
    import io
    from pathlib import Path

    path_obj = Path(path) if isinstance(path, str) else path

    if path_obj.exists() is False:
        raise ConfigMissingError(str(path_obj))

    with io.open(path_obj, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {path_obj}: {e}") from e

    return load_and_validate_config(raw, str(path_obj))


def resolve_grid_path(config: RootConfig) -> str:
    return str((CONFIG_PATH.parent / config.robot.grid.path).resolve())


def assert_config(config: RootConfig, requirements: Optional[List[str]] = None) -> None:
    errors: List[str] = []

    if requirements is None:
        requirements = []

    for req in requirements:
        try:
            value = config.get(req)
            if value is None:
                errors.append(f"Required config '{req}' is None")
        except KeyError:
            errors.append(f"Required config '{req}' is missing")

    if config.auth.jwt_secret is None and config.auth.require_auth is True:
        errors.append("auth.jwt_secret is required when auth.require_auth is True")

    if config.robot_app.enabled is True:
        if config.robot.bus is None:
            errors.append("robot.bus is required when robot is enabled")

    if config.robot.tool.laser.enabled is True:
        if config.robot.bus is None:
            errors.append("robot.bus is required when robot.tool.laser.enabled is True")
        elif config.robot.bus.laser is None:
            errors.append("robot.bus.laser is required when robot.tool.laser.enabled is True")

    if errors:
        raise ConfigError("Configuration assertion failed:\n  - " + "\n  - ".join(errors))
