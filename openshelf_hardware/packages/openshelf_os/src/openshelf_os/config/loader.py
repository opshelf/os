from __future__ import annotations

import io
import json

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from pydantic import ValidationError
from openshelf_os.config.exceptions import ConfigError, ConfigMissingError, ConfigRequirementError, ConfigValidationError
from openshelf_os.config.root import RootConfig
from openshelf_os.runtime_state import RuntimeStatePaths, paths_for_config_file


CONFIG_PATH = RuntimeStatePaths.current().config_path


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
    path_obj = Path(path) if isinstance(path, str) else path

    if path_obj.exists() is False:
        raise ConfigMissingError(str(path_obj))

    with io.open(path_obj, "r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid JSON in {path_obj}: {e}") from e

    return load_and_validate_config(raw, str(path_obj))


def resolve_grid_path(config: RootConfig, config_path: Union[str, Any] = CONFIG_PATH) -> str:
    path_obj = Path(config_path) if isinstance(config_path, str) else config_path
    runtime_paths = paths_for_config_file(path_obj)
    return str(runtime_paths.resolve_storage_path(config, config_path=path_obj))


def config_requirement_issues(config: RootConfig, requirements: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """Cross-section requirements the model does not enforce, as (dotted path, message) pairs."""
    issues: List[Tuple[str, str]] = []

    for req in requirements or []:
        try:
            if config.get(req) is None:
                issues.append((req, f"Required config '{req}' is None"))
        except KeyError:
            issues.append((req, f"Required config '{req}' is missing"))

    if config.auth.jwt_secret is None and config.auth.require_auth is True:
        issues.append(("auth.jwt_secret", "auth.jwt_secret is required when auth.require_auth is True"))

    if config.robot_app.enabled is True and config.robot.bus is None:
        issues.append(("robot.bus", "robot.bus is required when robot is enabled"))

    if config.robot.tool.laser.enabled is True:
        if config.robot.bus is None:
            issues.append(("robot.bus", "robot.bus is required when robot.tool.laser.enabled is True"))
        elif config.robot.bus.laser is None:
            issues.append(("robot.bus.laser", "robot.bus.laser is required when robot.tool.laser.enabled is True"))

    return issues


def assert_config(config: RootConfig, requirements: Optional[List[str]] = None) -> None:
    issues = config_requirement_issues(config, requirements)
    if issues:
        raise ConfigRequirementError(issues)
