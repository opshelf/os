# ruff: noqa: F401, F403, F405
from __future__ import annotations

from .core import *
from .motion import *
from .shelf_scan import *

async def run_io_box_scout(
	*,
	robot: Robot,
	scan_config: Any,
	io_box_interior_width_mm: float,
	start_x_mm: float = IO_BOX_DEFAULT_START_X_MM,
	start_y_mm: float = IO_BOX_DEFAULT_START_Y_MM,
	y_min_mm: float = 0.0,
	x_max_mm: float = IO_BOX_DEFAULT_X_MAX_MM,
	coarse_step_mm: float = IO_BOX_COARSE_STEP_MM,
	fine_step_mm: float = IO_BOX_FINE_STEP_MM,
	fine_margin_mm: float = IO_BOX_FINE_MARGIN_MM,
	settle_ms: int = 20,
	samples_per_point: int = 1,
	laser_offset_x_mm: float | None = None,
	laser_offset_y_mm: float | None = None,
	should_abort: Callable[[], bool] | None = None,
) -> IOBoxScoutResult:
	if io_box_interior_width_mm <= 0.0:
		raise ValueError("io_box_interior_width_mm must be greater than zero")

	if coarse_step_mm <= 0.0:
		raise ValueError("coarse_step_mm must be greater than zero")

	if fine_step_mm <= 0.0:
		raise ValueError("fine_step_mm must be greater than zero")

	if fine_margin_mm < 0.0:
		raise ValueError("fine_margin_mm must be zero or greater")

	if start_y_mm <= y_min_mm:
		raise ValueError("start_y_mm must be greater than y_min_mm")

	if x_max_mm <= start_x_mm:
		raise ValueError("x_max_mm must be greater than start_x_mm")

	if settle_ms < 0:
		raise ValueError("settle_ms must be zero or greater")

	if samples_per_point <= 0:
		raise ValueError("samples_per_point must be greater than zero")

	if laser_offset_x_mm is None:
		laser_offset_x_mm = require_scan_config_parameter(scan_config, "laser_offset_x_mm")
	else:
		laser_offset_x_mm = float(laser_offset_x_mm)

	if laser_offset_y_mm is None:
		laser_offset_y_mm = require_scan_config_parameter(scan_config, "laser_offset_y_mm")
	else:
		laser_offset_y_mm = float(laser_offset_y_mm)

	tool = robot._tool
	io_box = getattr(robot, "_io", None)

	if io_box is None:
		raise ValueError("IO box is not configured")

	did_attempt_door_motion = False
	x_limit_mm = float(start_x_mm) + float(io_box_interior_width_mm)
	x_scan_max_mm = min(float(x_max_mm), x_limit_mm)

	if x_scan_max_mm <= start_x_mm:
		raise ValueError("IO box X scan limit must be greater than start_x_mm")

	async def scan_y_line_with_brake_released(
		*,
		positions: list[float],
		fixed_mm: float,
		progress_label: str,
	) -> list[float | None]:
		is_y_brake_disabled = False

		try:
			await set_y_brake_for_scan_motion(tool, is_enabled=False)
			is_y_brake_disabled = True
			return await scan_line_positions(
				tool,
				axis="y",
				positions=positions,
				fixed_mm=fixed_mm,
				settle_ms=settle_ms,
				samples_per_point=samples_per_point,
				progress_label=progress_label,
				should_abort=should_abort,
			)
		finally:
			if is_y_brake_disabled is True:
				await set_y_brake_for_scan_motion(tool, is_enabled=True)

	try:
		raise_if_cancelled(should_abort)
		print_status("phase=io-box-calibration")
		await enter_scan_mode(tool, StorageModuleSide.LEFT, scan_config)
		raise_if_cancelled(should_abort)

		coarse_y_positions = build_axis_positions(start_y_mm, y_min_mm, coarse_step_mm)
		print_status(
			f"phase=io-box-coarse-y x={start_x_mm:.2f} "
			f"y=({start_y_mm:.2f},{y_min_mm:.2f}) step={coarse_step_mm:.2f}"
		)
		coarse_y_values = await scan_y_line_with_brake_released(
			positions=coarse_y_positions,
			fixed_mm=start_x_mm,
			progress_label="io_box_coarse_y",
		)
		rough_y_segment = find_first_positive_run_segment(
			values=coarse_y_values,
			positions=coarse_y_positions,
			step_mm=coarse_step_mm,
		)

		if rough_y_segment is None:
			raise ValueError("No bounded rough IO box Y band found")

		rough_y_center_mm = float(rough_y_segment.center_mm)
		print_status(
			f"io_box_rough_y_bounds=({rough_y_segment.start_mm:.2f},{rough_y_segment.end_mm:.2f}) "
			f"center_y={rough_y_center_mm:.2f}"
		)

		raise_if_cancelled(should_abort)
		print_status("phase=io-box-door-open")
		did_attempt_door_motion = True
		await io_box.open_door()
		raise_if_cancelled(should_abort)

		fine_y_min_mm = max(y_min_mm, rough_y_segment.start_mm - fine_margin_mm)
		fine_y_max_mm = min(start_y_mm, rough_y_segment.end_mm + fine_margin_mm)
		fine_y_positions = build_axis_positions(fine_y_max_mm, fine_y_min_mm, fine_step_mm)
		print_status(
			f"phase=io-box-fine-y x={start_x_mm:.2f} "
			f"y=({fine_y_max_mm:.2f},{fine_y_min_mm:.2f}) step={fine_step_mm:.2f}"
		)
		fine_y_values = await scan_y_line_with_brake_released(
			positions=fine_y_positions,
			fixed_mm=start_x_mm,
			progress_label="io_box_fine_y",
		)
		fine_y_segment = find_gap_run_containing_position(
			values=fine_y_values,
			positions=fine_y_positions,
			step_mm=fine_step_mm,
			seed_mm=rough_y_center_mm,
		)

		if fine_y_segment is None:
			raise ValueError("No open-door IO box Y gap found at the rough center")

		if is_line_segment_bounded(fine_y_segment, positions=fine_y_positions) is False:
			raise ValueError("Open-door IO box Y gap is not bounded; increase --io-box-fine-margin-mm")

		center_y_mm = float(fine_y_segment.center_mm)
		print_status(
			f"io_box_fine_y_bounds=({fine_y_segment.start_mm:.2f},{fine_y_segment.end_mm:.2f}) "
			f"center_y={center_y_mm:.2f}"
		)

		x_positions = build_axis_positions(start_x_mm, x_scan_max_mm, fine_step_mm)
		print_status(
			f"phase=io-box-fine-x y={center_y_mm:.2f} "
			f"x=({start_x_mm:.2f},{x_scan_max_mm:.2f}) step={fine_step_mm:.2f} "
			f"limit=io_box_width"
		)
		x_values = await scan_line_positions(
			tool,
			axis="x",
			positions=x_positions,
			fixed_mm=center_y_mm,
			settle_ms=settle_ms,
			samples_per_point=samples_per_point,
			progress_label="io_box_fine_x",
			should_abort=should_abort,
		)
		x_segment = find_gap_run_containing_position(
			values=x_values,
			positions=x_positions,
			step_mm=fine_step_mm,
			seed_mm=start_x_mm,
		)

		if x_segment is None:
			raise ValueError("No open-door IO box X gap found at the scout X position")

		if x_segment.end_idx >= len(x_positions) - 1:
			raise ValueError(
				"Open-door IO box right X boundary was not found before the interior width limit"
			)

		measured_x_min_mm = None

		if x_segment.start_idx > 0:
			measured_x_min_mm = float(x_segment.start_mm)

		measured_x_max_mm = float(x_segment.end_mm)
		derived_x_min_mm = measured_x_max_mm - float(io_box_interior_width_mm)
		laser_center_x_mm = float((derived_x_min_mm + measured_x_max_mm) / 2.0)
		final_y_segment = fine_y_segment
		final_y_refined = False

		measured_x_min_label = "none"

		if measured_x_min_mm is not None:
			measured_x_min_label = f"{measured_x_min_mm:.2f}"

		print_status(
			f"io_box_fine_x_bounds=({x_segment.start_mm:.2f},{x_segment.end_mm:.2f}) "
			f"measured_x_min={measured_x_min_label} "
			f"measured_x_max={measured_x_max_mm:.2f} "
			f"derived_x_min={derived_x_min_mm:.2f} laser_center_x={laser_center_x_mm:.2f}"
		)

		if laser_center_x_mm >= start_x_mm and laser_center_x_mm <= x_max_mm:
			print_status(
				f"phase=io-box-final-y x={laser_center_x_mm:.2f} "
				f"y=({fine_y_max_mm:.2f},{fine_y_min_mm:.2f}) step={fine_step_mm:.2f}"
			)
			final_y_values = await scan_y_line_with_brake_released(
				positions=fine_y_positions,
				fixed_mm=laser_center_x_mm,
				progress_label="io_box_final_y",
			)
			refined_y_segment = find_gap_run_containing_position(
				values=final_y_values,
				positions=fine_y_positions,
				step_mm=fine_step_mm,
				seed_mm=center_y_mm,
			)

			if refined_y_segment is not None and is_line_segment_bounded(refined_y_segment, positions=fine_y_positions) is True:
				final_y_segment = refined_y_segment
				center_y_mm = float(final_y_segment.center_mm)
				final_y_refined = True
				print_status(
					f"io_box_final_y_bounds=({final_y_segment.start_mm:.2f},{final_y_segment.end_mm:.2f}) "
					f"laser_center_y={center_y_mm:.2f}"
				)
			else:
				print_status("io_box_final_y_refine=skipped reason=boundary_not_found")
		else:
			print_status("io_box_final_y_refine=skipped reason=laser_center_x_out_of_scan_range")

		laser_center_y_mm = float(center_y_mm)
		axis_center_x_mm = laser_center_x_mm + float(laser_offset_x_mm)
		axis_center_y_mm = laser_center_y_mm + float(laser_offset_y_mm)
		calibration_low_x_mm = axis_center_x_mm
		calibration_high_x_mm = axis_center_x_mm
		calibration_low_y_mm = float(final_y_segment.start_mm) + float(laser_offset_y_mm) + IO_BOX_LOW_Y_OFFSET_MM
		calibration_high_y_mm = float(final_y_segment.end_mm) + float(laser_offset_y_mm) - IO_BOX_HIGH_Y_OFFSET_MM
		front_stage_extension_mm = await tool.get_param("STAGE_EXTENSION_SHELF_FRONT")

		if front_stage_extension_mm is None:
			raise ValueError("STAGE_EXTENSION_SHELF_FRONT parameter is not configured")

		stage_extension_mm = float(front_stage_extension_mm) + IO_BOX_STAGE_EXTENSION_FRONT_OFFSET_MM
		z_extension_mm = IO_BOX_Z_EXTENSION_MM

		print_status(
			f"io_box_calibration_points low=({calibration_low_x_mm:.2f},{calibration_low_y_mm:.2f}) "
			f"high=({calibration_high_x_mm:.2f},{calibration_high_y_mm:.2f}) "
			f"stage_extension_mm={stage_extension_mm:.2f} z_extension_mm={z_extension_mm:.2f}"
		)

		return IOBoxScoutResult(
			rough_y_min_mm=float(rough_y_segment.start_mm),
			rough_y_max_mm=float(rough_y_segment.end_mm),
			fine_y_min_mm=float(final_y_segment.start_mm),
			fine_y_max_mm=float(final_y_segment.end_mm),
			measured_x_min_mm=measured_x_min_mm,
			measured_x_max_mm=measured_x_max_mm,
			derived_x_min_mm=derived_x_min_mm,
			laser_center_x_mm=laser_center_x_mm,
			laser_center_y_mm=laser_center_y_mm,
			axis_center_x_mm=axis_center_x_mm,
			axis_center_y_mm=axis_center_y_mm,
			laser_offset_x_mm=float(laser_offset_x_mm),
			laser_offset_y_mm=float(laser_offset_y_mm),
			calibration_low_x_mm=calibration_low_x_mm,
			calibration_low_y_mm=calibration_low_y_mm,
			calibration_high_x_mm=calibration_high_x_mm,
			calibration_high_y_mm=calibration_high_y_mm,
			io_box_interior_width_mm=float(io_box_interior_width_mm),
			stage_extension_mm=stage_extension_mm,
			z_extension_mm=z_extension_mm,
			final_y_refined=final_y_refined,
		)
	finally:
		if should_skip_motion_cleanup_on_cancel() is False:
			await tool.retract()

			if did_attempt_door_motion is True:
				await io_box.home_door()
