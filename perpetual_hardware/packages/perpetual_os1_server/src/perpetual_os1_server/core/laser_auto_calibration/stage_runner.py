# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

from .models import *


class StageRunnerMixin:
    async def _run_stage_extension_calibration_for_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
        *,
        probe_y_mm: float,
    ) -> float:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        tool = worker._robot._tool

        if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
            raise ValueError("x, y, z, stage, and rotation motors are required")

        if probe_y_mm < bounds.y_min or probe_y_mm > bounds.y_max:
            raise ValueError(
                f"stage extension probe y {probe_y_mm:.3f} is outside bounds {bounds.y_min:.3f}..{bounds.y_max:.3f}"
            )

        side_name = _side_to_short_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        side_module_name = _side_to_module_name(side)
        rotation_param = _side_to_rotation_param_name(side)
        absolute_rotation_value = await tool.get_param(rotation_param)
        if absolute_rotation_value is None:
            raise ValueError(f"{rotation_param} is required")

        absolute_rotation_deg = float(absolute_rotation_value)
        z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
        z_move_rpm = await tool.get_param("Z_MOVE_RPM")
        z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

        if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
            raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

        probe_x_mm = float((bounds.x_min + bounds.x_max) / 2.0)
        laser_scan_z_offset_from_place_mm = laser_module.get_laser_scan_z_offset_from_place_mm(scan_config)
        base_z_mm = float(z_extension_place) + laser_scan_z_offset_from_place_mm
        stage_limit_mm = float(tool._ms._params.lim)
        probe_rpm = min(int(tool._ms._params.rpm), 100)

        await self._update_snapshot(
            current_side=side_module_name,
            current_stage="stage-extension-calibration",
            state="running",
            bounds=bounds,
            stage_detail={
                "side": side_module_name,
                "phase": "stage-extension-calibration",
                "subphase": "move",
                "probe_x_mm": probe_x_mm,
                "probe_y_mm": float(probe_y_mm),
                "absolute_rotation_deg": absolute_rotation_deg,
                "probe_rpm": probe_rpm,
                "stage_target_mm": stage_limit_mm,
            },
        )

        try:
            self._raise_if_abort_requested()
            await tool.retract()
            await angle_module.prepare_capture_pose(
                tool,
                side_name=side_name,
                side_number=side_number,
                x_mm=probe_x_mm,
                y_mm=float(probe_y_mm),
                absolute_rotation_deg=absolute_rotation_deg,
                stage_extension_mm=STAGE_EXTENSION_PROBE_START_MM,
                z_mm=base_z_mm,
                z_move_rpm=float(z_move_rpm),
                z_move_accel=float(z_move_accel),
                should_rotate=True,
                should_use_safe_right_rotation_path=True,
                should_move_z=False,
            )
            self._raise_if_abort_requested()
            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "probe",
                    "probe_x_mm": probe_x_mm,
                    "probe_y_mm": float(probe_y_mm),
                    "absolute_rotation_deg": absolute_rotation_deg,
                    "probe_rpm": probe_rpm,
                    "stage_target_mm": stage_limit_mm,
                },
            )
            did_stall, stage_position_mm, status, alarm_code = await angle_module.probe_stage_until_stall(
                tool,
                target_stage_mm=stage_limit_mm,
                retract_stage_mm=STAGE_EXTENSION_PROBE_START_MM,
                settle_ms=int(laser_module.get_laser_sample_settle_ms(scan_config)),
                timeout_s=STAGE_EXTENSION_PROBE_TIMEOUT_S,
                probe_rpm=probe_rpm,
                no_progress_s=STAGE_EXTENSION_NO_PROGRESS_TIMEOUT_S,
                probe_step_mm=STAGE_EXTENSION_PROBE_STEP_MM,
                fast_approach_mm=STAGE_EXTENSION_PROBE_START_MM,
                fast_approach_rpm=int(tool._ms._params.rpm),
                auto_retract=False,
                should_abort=self._should_abort_work,
            )
            self._raise_if_abort_requested()

            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "probe-result",
                    "probe_x_mm": probe_x_mm,
                    "probe_y_mm": float(probe_y_mm),
                    "absolute_rotation_deg": absolute_rotation_deg,
                    "stall_status": status,
                    "alarm_code": alarm_code,
                    "stall_stage_mm": float(stage_position_mm),
                    "stage_target_mm": stage_limit_mm,
                },
            )

            if status == "timeout":
                raise ValueError(
                    f"stage extension probe timed out before stall at {stage_position_mm:.3f} mm"
                )

            if did_stall is False:
                raise ValueError(
                    f"stage extension probe reached limit without stall at {stage_position_mm:.3f} mm"
                )

            calibrated_stage_extension_mm = max(
                0.0,
                float(math.floor(stage_position_mm)) - STAGE_EXTENSION_STALL_BACKOFF_MM,
            )
            self._raise_if_abort_requested()
            await tool._ms.move_to_position(calibrated_stage_extension_mm, rpm=probe_rpm)
            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "backoff",
                    "probe_x_mm": probe_x_mm,
                    "probe_y_mm": float(probe_y_mm),
                    "absolute_rotation_deg": absolute_rotation_deg,
                    "stall_status": status,
                    "alarm_code": alarm_code,
                    "stall_stage_mm": float(stage_position_mm),
                    "stage_backoff_mm": STAGE_EXTENSION_STALL_BACKOFF_MM,
                    "calibrated_stage_extension_mm": calibrated_stage_extension_mm,
                },
            )
            return calibrated_stage_extension_mm
        finally:
            if _should_skip_motion_cleanup_on_cancel() is False:
                await tool.retract()

    async def _resolve_stage_extension_probe_y_mm(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
        *,
        top_shelf_y_mm: float | None = None,
    ) -> float:
        side_name = _side_to_short_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        side_module_name = _side_to_module_name(side)

        if top_shelf_y_mm is None:
            config = get_root_config()
            scan_config = config.robot.grid.scan
            tool = worker._robot._tool

            if tool._mx is None or tool._my is None or tool._mg is None or tool._ms is None or tool._mr is None:
                raise ValueError("x, y, z, stage, and rotation motors are required")

            z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
            z_move_rpm = await tool.get_param("Z_MOVE_RPM")
            z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

            if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
                raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

            x_positions = _build_angle_x_positions(scan_config)
            laser_scan_stage_extension_mm = laser_module.get_laser_scan_stage_extension_mm(scan_config)
            laser_scan_z_offset_from_place_mm = laser_module.get_laser_scan_z_offset_from_place_mm(scan_config)
            absolute_rotation_value = await tool.get_param(_side_to_rotation_param_name(side))
            if absolute_rotation_value is None:
                raise ValueError(f"{_side_to_rotation_param_name(side)} is required")

            absolute_rotation_deg = float(absolute_rotation_value)
            base_z_mm = float(z_extension_place) + laser_scan_z_offset_from_place_mm
            angle_args = argparse.Namespace(
                y_mm=None,
                y_range=(bounds.y_min, bounds.y_max),
                scout_x_mm=None,
            )

            await self._update_snapshot(
                current_side=side_module_name,
                current_stage="stage-extension-calibration",
                state="running",
                bounds=bounds,
                stage_detail={
                    "side": side_module_name,
                    "phase": "stage-extension-calibration",
                    "subphase": "shelf-scout",
                },
            )

            await tool.retract()
            top_shelf_y_mm = await angle_module.resolve_capture_y_mm(
                tool,
                args=angle_args,
                scan_config=scan_config,
                side_name=side_name,
                side_number=side_number,
                x_positions=x_positions,
                rotation_center_deg=absolute_rotation_deg,
                stage_extension_mm=laser_scan_stage_extension_mm,
                base_z_mm=base_z_mm,
                z_move_rpm=float(z_move_rpm),
                z_move_accel=float(z_move_accel),
                settle_ms=int(laser_module.get_laser_sample_settle_ms(scan_config)),
                samples_per_point=int(laser_module.get_laser_samples_per_point(scan_config)),
                should_abort=self._should_abort_work,
            )

        probe_y_mm = _build_stage_extension_probe_y_mm(float(top_shelf_y_mm), bounds)
        await self._update_snapshot(
            current_side=side_module_name,
            current_stage="stage-extension-calibration",
            state="running",
            bounds=bounds,
            stage_detail={
                "side": side_module_name,
                "phase": "stage-extension-calibration",
                "subphase": "probe-y",
                "top_shelf_y_mm": float(top_shelf_y_mm),
                "probe_y_mm": probe_y_mm,
            },
        )
        return probe_y_mm

    async def _run_io_box_calibration(self, worker: RobotWorker) -> None:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        laser_offset_x_mm = _resolve_io_box_laser_offset_x_mm(scan_config)
        laser_offset_y_mm = laser_module.require_scan_config_parameter(scan_config, "laser_offset_y_mm")

        await self._update_snapshot(
            current_side=None,
            current_stage="io-box-calibration",
            state="running",
            stage_detail={
                "phase": "io-box-calibration",
                "subphase": "scan",
                "laser_offset_x_mm": laser_offset_x_mm,
                "laser_offset_y_mm": laser_offset_y_mm,
            },
        )

        result = await laser_module.run_io_box_scout(
            robot=worker._robot,
            scan_config=scan_config,
            io_box_interior_width_mm=laser_module.IO_BOX_INTERIOR_WIDTH_MM,
            settle_ms=laser_module.get_fine_settle_ms(
                laser_module.get_laser_sample_settle_ms(scan_config)
            ),
            samples_per_point=laser_module.get_fine_samples_per_point(
                laser_module.get_laser_samples_per_point(scan_config)
            ),
            laser_offset_x_mm=laser_offset_x_mm,
            laser_offset_y_mm=laser_offset_y_mm,
            should_abort=self._should_abort_work,
        )

        io_box_result = LaserAutoCalibrationIOBoxResult(
            pickup_low_x=float(result.calibration_low_x_mm),
            pickup_low_y=float(result.calibration_low_y_mm),
            pickup_high_x=float(result.calibration_high_x_mm),
            pickup_high_y=float(result.calibration_high_y_mm),
            z_extension=float(result.z_extension_mm),
            stage_extension=float(result.stage_extension_mm),
        )

        await self._update_snapshot(
            current_side=None,
            current_stage="io-box-calibration",
            state="running",
            io_box_result=io_box_result,
            stage_detail={
                "phase": "io-box-calibration",
                "subphase": "apply",
                "pickup_low_x": io_box_result.pickup_low_x,
                "pickup_low_y": io_box_result.pickup_low_y,
                "pickup_high_x": io_box_result.pickup_high_x,
                "pickup_high_y": io_box_result.pickup_high_y,
                "z_extension": io_box_result.z_extension,
                "stage_extension": io_box_result.stage_extension,
            },
        )

        next_config = config.model_copy(deep=True)
        params_dict = next_config.robot.tool.parameters.model_dump()
        params_dict["PICKUP_POSITION_L"] = (int(io_box_result.pickup_low_x), int(io_box_result.pickup_low_y))
        params_dict["PICKUP_POSITION_H"] = (int(io_box_result.pickup_high_x), int(io_box_result.pickup_high_y))
        params_dict["Z_EXTENSION_IO"] = io_box_result.z_extension
        params_dict["STAGE_EXTENSION_IO_BOX"] = io_box_result.stage_extension
        next_config.robot.tool.parameters = Parameters.model_validate(params_dict)

        save_root_config(next_config)
        clear_config_cache()
        worker._robot.reload_parameters()

    async def _apply_stage_extension_calibration_in_memory(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        stage_extension_mm: float,
    ) -> None:
        stage_extension_param = _side_to_stage_extension_param_name(side)
        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict[stage_extension_param] = stage_extension_mm
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)
        side_name = _side_to_module_name(side)
        self._side_states.setdefault(side_name, _SideState()).stage_extension_mm = stage_extension_mm

        await self._update_snapshot(
            current_side=side_name,
            current_stage="stage-extension-calibration",
            state="running",
            stage_detail={
                "side": side_name,
                "phase": "stage-extension-applied",
                "stage_extension_param": stage_extension_param,
                "calibrated_stage_extension_mm": float(stage_extension_mm),
            },
        )

