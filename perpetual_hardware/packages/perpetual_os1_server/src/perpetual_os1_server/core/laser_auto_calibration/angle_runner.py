# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import argparse

from .models import *


class AngleRunnerMixin:
    async def _run_angle_calibration_for_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
    ) -> LaserAutoCalibrationAngleResult:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        tool = worker._robot._tool

        if tool._laser_enabled is False or tool._laser is None:
            raise ValueError("Laser is not enabled")

        if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
            raise ValueError("x, y, z, stage, and rotation motors are required")

        side_name = _side_to_short_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        x_positions = _build_angle_x_positions(scan_config)
        z_offset_positions = _build_angle_z_offset_positions(scan_config)
        settle_ms = int(laser_module.get_laser_sample_settle_ms(scan_config))
        samples_per_point = int(laser_module.get_laser_samples_per_point(scan_config))
        stage_extension_mm = laser_module.get_laser_scan_stage_extension_mm(scan_config)
        laser_scan_z_offset_from_place_mm = laser_module.get_laser_scan_z_offset_from_place_mm(scan_config)
        rotation_param = _side_to_rotation_param_name(side)

        previous_rotation_value = await tool.get_param(rotation_param)
        if previous_rotation_value is None:
            raise ValueError(f"{rotation_param} is required")

        previous_rotation_deg = float(previous_rotation_value)
        rotation_center_deg = previous_rotation_deg
        rotation_limit_deg = float(tool._mr._params.lim)
        rotation_positions = _build_valid_rotation_positions(
            scan_config,
            rotation_center_deg,
            rotation_limit_deg,
        )
        rotation_positions = _build_angle_rotation_positions_for_side(
            side,
            rotation_positions,
            rotation_center_deg,
            rotation_limit_deg,
        )
        z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
        z_move_rpm = await tool.get_param("Z_MOVE_RPM")
        z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

        if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
            raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

        base_z_mm = float(z_extension_place) + laser_scan_z_offset_from_place_mm
        angle_args = argparse.Namespace(
            y_mm=None,
            y_range=(bounds.y_min, bounds.y_max),
            scout_x_mm=None,
        )
        samples: list[Any] = []

        async with self._patch_status_callbacks(
            scan_callback=None,
            angle_callback=self._build_angle_status_callback(_side_to_module_name(side)),
        ):
            try:
                self._raise_if_abort_requested()
                await tool.retract()
                capture_y_mm = await angle_module.resolve_capture_y_mm(
                    tool,
                    args=angle_args,
                    scan_config=scan_config,
                    side_name=side_name,
                    side_number=side_number,
                    x_positions=x_positions,
                    rotation_center_deg=rotation_center_deg,
                    stage_extension_mm=stage_extension_mm,
                    base_z_mm=base_z_mm,
                    z_move_rpm=float(z_move_rpm),
                    z_move_accel=float(z_move_accel),
                    settle_ms=settle_ms,
                    samples_per_point=samples_per_point,
                    should_abort=self._should_abort_work,
                )
                self._raise_if_abort_requested()
                current_z_mm = None
                current_absolute_rotation_deg: float | None = rotation_center_deg

                for z_offset_mm in z_offset_positions:
                    self._raise_if_abort_requested()
                    target_z_mm = base_z_mm + float(z_offset_mm)

                    for rotation_idx, rotation_deg in enumerate(rotation_positions, start=1):
                        self._raise_if_abort_requested()
                        absolute_rotation_deg = rotation_center_deg + float(rotation_deg)

                        for x_idx, x_mm in enumerate(x_positions, start=1):
                            self._raise_if_abort_requested()
                            angle_module.print_status(
                                f"phase=capture x={x_idx}/{len(x_positions)} z={z_offset_mm:.2f} "
                                f"rotation={rotation_idx}/{len(rotation_positions)} "
                                f"absolute_rotation_deg={absolute_rotation_deg:.3f}"
                            )
                            await angle_module.prepare_capture_pose(
                                tool,
                                side_name=side_name,
                                side_number=side_number,
                                x_mm=float(x_mm),
                                y_mm=capture_y_mm,
                                absolute_rotation_deg=absolute_rotation_deg,
                                stage_extension_mm=stage_extension_mm,
                                z_mm=target_z_mm,
                                z_move_rpm=float(z_move_rpm),
                                z_move_accel=float(z_move_accel),
                                should_rotate=current_absolute_rotation_deg != absolute_rotation_deg,
                                should_use_safe_right_rotation_path=True,
                                should_move_z=current_z_mm != target_z_mm,
                            )
                            self._raise_if_abort_requested()
                            current_z_mm = target_z_mm
                            current_absolute_rotation_deg = absolute_rotation_deg
                            depth_mm = await angle_module.sample_laser_depth(
                                tool,
                                settle_ms=settle_ms,
                                samples_per_point=samples_per_point,
                            )
                            self._raise_if_abort_requested()

                            if depth_mm is None:
                                angle_module.print_status(
                                    f"sample=invalid x={x_mm:.3f} z={target_z_mm:.3f} rotation_deg={rotation_deg:.3f}"
                                )
                                continue

                            samples.append(
                                _build_angle_calibration_sample(
                                    x_mm=float(x_mm),
                                    target_z_mm=target_z_mm,
                                    rotation_deg=float(rotation_deg),
                                    absolute_rotation_deg=float(absolute_rotation_deg),
                                    stage_extension_mm=stage_extension_mm,
                                    depth_mm=float(depth_mm),
                                )
                            )
                            angle_module.print_status(
                                f"sample=ok x={x_mm:.3f} z={target_z_mm:.3f} rotation_deg={rotation_deg:.3f} distance_mm={depth_mm:.3f}"
                            )
                self._raise_if_abort_requested()
                fit_samples, laser_offset_x_mm = _build_angle_fit_inputs(side, scan_config, samples, rotation_center_deg)
                fit_result = angle_module.search_best_alpha(
                    fit_samples,
                    alpha_min_deg=float(scan_config.angle_calibration_alpha_min_deg),
                    alpha_max_deg=float(scan_config.angle_calibration_alpha_max_deg),
                    coarse_step_deg=float(scan_config.angle_calibration_coarse_step_deg),
                    fine_window_deg=float(scan_config.angle_calibration_fine_window_deg),
                    fine_step_deg=float(scan_config.angle_calibration_fine_step_deg),
                    laser_offset_x_mm=laser_offset_x_mm,
                    laser_offset_z_mm=0.0,
                    beam_zero_deg=float(scan_config.angle_calibration_beam_zero_deg),
                )
                angle_module.print_fit_result(fit_result)
            finally:
                if _should_skip_motion_cleanup_on_cancel() is False:
                    await tool.retract()

        updated_rotation_deg = _build_estimated_rotation_deg(rotation_center_deg, fit_result)

        if updated_rotation_deg < 0.0 or updated_rotation_deg > rotation_limit_deg:
            raise ValueError(
                "estimated rotation is outside motor range "
                f"({updated_rotation_deg:.3f} deg is outside 0..{rotation_limit_deg:.3f})"
            )

        return LaserAutoCalibrationAngleResult(
            side=_side_to_module_name(side),
            alpha_deg=float(fit_result.alpha_deg),
            sample_count=int(fit_result.sample_count),
            capture_y_mm=float(capture_y_mm),
            rms_residual_mm=float(fit_result.line.rms_residual_mm),
            max_abs_residual_mm=float(fit_result.line.max_abs_residual_mm),
            line_angle_deg=float(fit_result.line.line_angle_deg),
            previous_rotation_deg=previous_rotation_deg,
            updated_rotation_deg=updated_rotation_deg,
        )

    async def _apply_angle_calibration_in_memory(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        result: LaserAutoCalibrationAngleResult,
    ) -> None:
        rotation_param = _side_to_rotation_param_name(side)
        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict[rotation_param] = result.updated_rotation_deg
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)
        side_name = _side_to_module_name(side)
        self._side_states.setdefault(side_name, _SideState()).angle_result = result

        await self._update_snapshot(
            current_side=side_name,
            current_stage="angle-calibration",
            state="running",
            stage_detail={
                "side": side_name,
                "phase": "angle-applied",
                "alpha_deg": result.alpha_deg,
                "updated_rotation_deg": result.updated_rotation_deg,
            },
        )

