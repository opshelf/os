from __future__ import annotations

from importlib import import_module
from typing import Any

_MODULE_NAMES = (
    "core",
    "recording",
    "motion",
    "shelf_scan",
    "io_box",
    "fine_scan",
    "staged_scan",
)

_PATCH_TARGET_NAMES = (
    "Robot",
    "build_standalone_config",
    "enter_scan_mode",
    "capture_shelf_candidates",
    "persist_staged_metadata",
    "persist_scan_started_at",
    "set_y_brake_for_scan_motion",
    "capture_cross_scan",
    "scan_line_positions",
    "build_shelves_from_scan",
    "apply_wall_depth_z_offsets",
    "print_status",
)

_ORIGINAL_EXPORTS: dict[str, Any] = {}
_WRAPPER_EXPORTS: dict[str, Any] = {}


def _load_module(name: str) -> Any:
    return import_module(f"{__name__}.{name}")


def _get_original_export(name: str) -> Any:
    if name in _ORIGINAL_EXPORTS:
        return _ORIGINAL_EXPORTS[name]

    for module_name in _MODULE_NAMES:
        module = _load_module(module_name)

        if hasattr(module, name):
            value = getattr(module, name)
            _ORIGINAL_EXPORTS[name] = value
            return value

    return None


def _get_patch_target_value(name: str) -> Any:
    value = globals().get(name, None)

    wrapper = _WRAPPER_EXPORTS.get(name)

    if wrapper is not None and value is wrapper:
        return _get_original_export(name)

    return value if value is not None else _get_original_export(name)


def _sync_patch_targets() -> None:
    modules = [_load_module(name) for name in _MODULE_NAMES]

    for module in modules:
        for name in _PATCH_TARGET_NAMES:
            _get_original_export(name)
            value = _get_patch_target_value(name)

            if value is not None:
                setattr(module, name, value)

def _find_export(name: str) -> Any:
    if name in _ORIGINAL_EXPORTS:
        value = _ORIGINAL_EXPORTS[name]
        globals()[name] = value
        return value

    for module_name in _MODULE_NAMES:
        module = _load_module(module_name)

        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def run_with_temp_robot_config(*args: Any, **kwargs: Any) -> Any:
    _sync_patch_targets()
    return await _load_module("core").run_with_temp_robot_config(*args, **kwargs)


async def capture_shelf_candidates(*args: Any, **kwargs: Any) -> Any:
    implementation = _get_original_export("capture_shelf_candidates")
    _sync_patch_targets()
    return await implementation(*args, **kwargs)


async def run_io_box_scout(*args: Any, **kwargs: Any) -> Any:
    _sync_patch_targets()
    return await _load_module("io_box").run_io_box_scout(*args, **kwargs)


async def run_staged_scan(*args: Any, **kwargs: Any) -> Any:
    _sync_patch_targets()
    return await _load_module("staged_scan").run_staged_scan(*args, **kwargs)


def apply_single_shelf_analysis(*args: Any, **kwargs: Any) -> Any:
    _sync_patch_targets()
    return _load_module("staged_scan").apply_single_shelf_analysis(*args, **kwargs)


_WRAPPER_EXPORTS["capture_shelf_candidates"] = capture_shelf_candidates


def __getattr__(name: str) -> Any:
    return _find_export(name)
