# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .models import *


def _write_stage_extension_shelf_scout_debug_document(
    *,
    side_name: str,
    absolute_rotation_deg: float,
    debug_document: dict[str, Any],
    error: Exception,
    bounds: ScanBounds,
    probe_x_positions: list[float],
) -> Path:
    timestamp = _get_timestamp().replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    output_path = Path(_build_default_scans_dir()) / ANGLE_DEBUG_DIR_NAME / f"{side_name}-stage-extension-shelf-scout-{timestamp}.json"
    document = dict(debug_document)
    metadata = dict(document.get("metadata", {}))
    metadata.update(
        {
            "side": side_name,
            "absolute_rotation_deg": float(absolute_rotation_deg),
            "probe_x_positions": [float(value) for value in probe_x_positions],
            "bounds": {
                "x_min": float(bounds.x_min),
                "x_max": float(bounds.x_max),
                "y_min": float(bounds.y_min),
                "y_max": float(bounds.y_max),
            },
            "error": str(error),
        }
    )
    document["metadata"] = metadata
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(document, indent="\t"), encoding="utf-8")
    return output_path


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
        laser_scan_pose_z_delta_from_place_mm = laser_module.get_laser_scan_pose_z_delta_from_place_mm(scan_config)
        base_z_mm = float(z_extension_place) + laser_scan_pose_z_delta_from_place_mm
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
            laser_scan_pose_stage_mm = laser_module.get_laser_scan_pose_stage_mm(scan_config)
            laser_scan_pose_z_delta_from_place_mm = laser_module.get_laser_scan_pose_z_delta_from_place_mm(scan_config)
            absolute_rotation_value = await tool.get_param(_side_to_rotation_param_name(side))
            if absolute_rotation_value is None:
                raise ValueError(f"{_side_to_rotation_param_name(side)} is required")

            absolute_rotation_deg = float(absolute_rotation_value)
            base_z_mm = float(z_extension_place) + laser_scan_pose_z_delta_from_place_mm
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
            try:
                top_shelf_y_mm = await angle_module.resolve_capture_y_mm(
                    tool,
                    args=angle_args,
                    scan_config=scan_config,
                    side_name=side_name,
                    side_number=side_number,
                    x_positions=x_positions,
                    rotation_center_deg=absolute_rotation_deg,
                    stage_extension_mm=laser_scan_pose_stage_mm,
                    base_z_mm=base_z_mm,
                    z_move_rpm=float(z_move_rpm),
                    z_move_accel=float(z_move_accel),
                    settle_ms=int(laser_module.get_laser_sample_settle_ms(scan_config)),
                    samples_per_point=int(laser_module.get_laser_samples_per_point(scan_config)),
                    should_abort=self._should_abort_work,
                )
            except angle_module.ShelfScoutError as exc:
                try:
                    debug_path = _write_stage_extension_shelf_scout_debug_document(
                        side_name=side_module_name,
                        absolute_rotation_deg=absolute_rotation_deg,
                        debug_document=exc.debug_document,
                        error=exc,
                        bounds=bounds,
                        probe_x_positions=x_positions,
                    )
                    angle_module.print_status(f"shelf_scout=debug path={debug_path}")
                except Exception as debug_exc:
                    logger.warning("Failed to write stage-extension shelf-scout debug document: %s", debug_exc)

                raise

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

    async def _apply_stage_extension_calibration(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        stage_extension_mm: float,
    ) -> None:
        stage_extension_param = _side_to_stage_extension_param_name(side)
        self._persist_tool_parameter(worker, stage_extension_param, stage_extension_mm)
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

