# mypy: disable-error-code=no-redef
# ruff: noqa: F401, F403, F405
from __future__ import annotations

from . import angle_runner as _angle_runner
from . import apply_runner as _apply_runner
from . import manager as _manager
from . import models as _models
from . import scan_runner as _scan_runner
from . import scope_runner as _scope_runner
from . import snapshot_runner as _snapshot_runner
from . import stage_runner as _stage_runner
from .manager import LaserAutoCalibrationManager as _LaserAutoCalibrationManagerImpl
from .models import *

_PATCH_TARGETS = [
    _models,
    _manager,
    _scope_runner,
    _scan_runner,
    _angle_runner,
    _stage_runner,
    _apply_runner,
    _snapshot_runner,
]
_PATCHED_NAMES = [
    "get_config_path",
    "get_root_config",
    "save_root_config",
    "clear_config_cache",
    "resolve_grid_path",
    "_build_bounds_for_side",
    "_build_angle_x_positions",
    "_build_valid_rotation_positions",
    "_build_angle_z_offset_positions",
    "_build_angle_rotation_positions_for_side",
    "laser_module",
    "angle_module",
]


def _sync_patch_targets() -> None:
    for module in _PATCH_TARGETS:
        for name in _PATCHED_NAMES:
            if name in globals():
                setattr(module, name, globals()[name])


_build_default_scans_dir_impl = _models._build_default_scans_dir


def _build_default_scans_dir() -> str:
    _sync_patch_targets()
    return _build_default_scans_dir_impl()


class LaserAutoCalibrationManager(_LaserAutoCalibrationManagerImpl):
    def __init__(self) -> None:
        _sync_patch_targets()
        super().__init__()

    async def ensure_session_started(self, *args, **kwargs):
        _sync_patch_targets()
        return await super().ensure_session_started(*args, **kwargs)

    async def _run_session(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_session(*args, **kwargs)

    async def _run_requested_scope(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_requested_scope(*args, **kwargs)

    async def _run_all_sides(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_all_sides(*args, **kwargs)

    async def _run_single_side(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_single_side(*args, **kwargs)

    async def _run_single_shelf(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_single_shelf(*args, **kwargs)

    async def _run_single_cell(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_single_cell(*args, **kwargs)

    async def _run_scan_analysis(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_scan_analysis(*args, **kwargs)

    async def _run_angle_calibration_for_side(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_angle_calibration_for_side(*args, **kwargs)

    async def _run_stage_extension_calibration_for_side(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_stage_extension_calibration_for_side(*args, **kwargs)

    async def _resolve_stage_extension_probe_y_mm(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._resolve_stage_extension_probe_y_mm(*args, **kwargs)

    async def _run_io_box_calibration(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._run_io_box_calibration(*args, **kwargs)

    async def _persist_completed_results(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._persist_completed_results(*args, **kwargs)

    async def _persist_single_shelf_result(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._persist_single_shelf_result(*args, **kwargs)

    async def _persist_single_cell_result(self, *args, **kwargs):
        _sync_patch_targets()
        return await super()._persist_single_cell_result(*args, **kwargs)


_laser_auto_calibration_manager = LaserAutoCalibrationManager()


def get_laser_auto_calibration_manager() -> LaserAutoCalibrationManager:
    return _laser_auto_calibration_manager


async def is_laser_auto_calibration_running() -> bool:
    return await _laser_auto_calibration_manager.is_active()
