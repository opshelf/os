# ruff: noqa: F401
from __future__ import annotations

from .capture import (
	enter_capture_mode,
	get_default_rotation_center_deg,
	prepare_capture_pose,
	resolve_capture_y_mm,
	resolve_rotation_center_deg,
)
from .cli import (
	main,
	parse_args,
	parse_pair_argument,
	parse_range_argument,
	run_stage_stall_async,
	validate_args,
)
from .config import (
	build_standalone_config,
	get_laser_sample_settle_ms,
	get_laser_samples_per_point,
	get_laser_scan_stage_extension_mm,
	get_laser_scan_z_offset_from_place_mm,
)
from .constants import (
	GAP_DEPTH_THRESHOLD_MM,
	LASER_SCAN_STAGE_EXTENSION_MM,
	LASER_SCAN_Z_OFFSET_MM,
	LEFT_MODULE_ROTATION_CENTER_DEG,
	RIGHT_MODULE_ROTATION_CENTER_DEG,
	RIGHT_MODULE_ROTATION_SAFE_X_MM,
	RIGHT_MODULE_SHELF_SCOUT_X_MM,
	ROTATION_SAFE_X_MM,
	SHELF_BRIM_MAX_HEIGHT_MM,
	SHELF_BRIM_MIN_HEIGHT_MM,
	SHELF_EDGE_CONFIRMATION_COUNT,
	SHELF_EDGE_TOP_MARGIN_MM,
	SHELF_MIN_SPACING_MM,
	SHELF_SCOUT_POST_EDGE_SKIP_MM,
	SHELF_SCOUT_STEP_MM,
	SHELF_SCOUT_X_OFFSETS_MM,
)
from .geometry import (
	build_axis_positions,
)
from .models import (
	ShelfSurfaceEdge,
	StageStallProbeSample,
)
from .reporting import (
	write_stage_stall_output_document,
)
from .robot_ops import (
	move_gantry_direct,
	probe_stage_until_stall,
	run_with_temp_robot_config,
	sample_laser_depth,
)
from .shelf_scout import (
	build_shelf_scout_x_positions,
	filter_shelf_edges,
	scan_shelf_scout_line,
	set_y_brake_for_shelf_scouting,
)
from .stage_stall import (
	finish_stage_stall_capture,
	resolve_probe_x_mm,
	resolve_stage_retract_target,
	resolve_stage_stall_fast_approach_mm,
	retract_stage_after_stall,
	run_stage_stall_capture,
	should_stop_after_stage_drop,
	summarize_stage_stall_samples,
)
from .status import (
	get_current_timestamp,
	print_status,
	raise_if_cancelled,
	set_status_printer,
	should_skip_motion_cleanup_on_cancel,
)
from .utils import depth_to_label, is_positive_depth
