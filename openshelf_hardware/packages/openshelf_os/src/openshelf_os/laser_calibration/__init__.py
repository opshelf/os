from __future__ import annotations

from contextlib import contextmanager
from importlib import import_module
from typing import Any, Iterator

_MODULE_NAMES = (
    "core",
    "recording",
    "motion",
    "shelf_scan",
    "fine_scan",
    "staged_scan",
    "reference_offsets",
    "z_stall",
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
_SYNC_DEPTH = 0


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


def _restore_patch_targets() -> None:
    modules = [_load_module(name) for name in _MODULE_NAMES]

    for module in modules:
        for name in _PATCH_TARGET_NAMES:
            value = _get_original_export(name)

            if value is not None:
                setattr(module, name, value)


@contextmanager
def _synced_patch_targets() -> Iterator[None]:
    """Propagated patches must not outlive the call that needed them.

    Callers patch package-level names, so the current values are pushed into the
    submodules for the duration of the call and reverted afterwards. Without the
    revert, a mock removed by its own context manager stays bound inside the
    submodules for the rest of the process. Entry points nest (a staged scan runs
    inside run_with_temp_robot_config), so only the outermost call reverts.
    """
    global _SYNC_DEPTH

    _sync_patch_targets()
    _SYNC_DEPTH += 1

    try:
        yield
    finally:
        _SYNC_DEPTH -= 1

        if _SYNC_DEPTH == 0:
            _restore_patch_targets()


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
    with _synced_patch_targets():
        return await _load_module("core").run_with_temp_robot_config(*args, **kwargs)


async def capture_shelf_candidates(*args: Any, **kwargs: Any) -> Any:
    implementation = _get_original_export("capture_shelf_candidates")

    with _synced_patch_targets():
        return await implementation(*args, **kwargs)


async def run_staged_scan(*args: Any, **kwargs: Any) -> Any:
    with _synced_patch_targets():
        return await _load_module("staged_scan").run_staged_scan(*args, **kwargs)


def apply_single_shelf_analysis(*args: Any, **kwargs: Any) -> Any:
    with _synced_patch_targets():
        return _load_module("staged_scan").apply_single_shelf_analysis(*args, **kwargs)


_WRAPPER_EXPORTS["capture_shelf_candidates"] = capture_shelf_candidates


def __getattr__(name: str) -> Any:
    return _find_export(name)
