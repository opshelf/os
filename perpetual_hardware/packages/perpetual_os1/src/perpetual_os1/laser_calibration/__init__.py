# ruff: noqa: F401, F403, F405
from __future__ import annotations

from . import core as _core
from . import fine_scan as _fine_scan
from . import io_box as _io_box
from . import motion as _motion
from . import recording as _recording
from . import shelf_scan as _shelf_scan
from . import staged_scan as _staged_scan
from .core import *
from .recording import *
from .motion import *
from .shelf_scan import *
from .io_box import *
from .fine_scan import *
from .staged_scan import *

_run_with_temp_robot_config_impl = _core.run_with_temp_robot_config
_capture_shelf_candidates_impl = _shelf_scan.capture_shelf_candidates
_run_io_box_scout_impl = _io_box.run_io_box_scout
_run_staged_scan_impl = _staged_scan.run_staged_scan
_apply_single_shelf_analysis_impl = _staged_scan.apply_single_shelf_analysis


def _sync_patch_targets() -> None:
	for module in [_core, _recording, _motion, _shelf_scan, _io_box, _fine_scan, _staged_scan]:
		for name in [
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
		]:
			if name in globals():
				setattr(module, name, globals()[name])


async def run_with_temp_robot_config(*args, **kwargs):
	_sync_patch_targets()
	return await _run_with_temp_robot_config_impl(*args, **kwargs)


async def capture_shelf_candidates(*args, **kwargs):
	_sync_patch_targets()
	return await _capture_shelf_candidates_impl(*args, **kwargs)


async def run_io_box_scout(*args, **kwargs):
	_sync_patch_targets()
	return await _run_io_box_scout_impl(*args, **kwargs)


async def run_staged_scan(*args, **kwargs):
	_sync_patch_targets()
	return await _run_staged_scan_impl(*args, **kwargs)


def apply_single_shelf_analysis(*args, **kwargs):
	_sync_patch_targets()
	return _apply_single_shelf_analysis_impl(*args, **kwargs)
