# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openshelf_os.laser_calibration.defaults import (
    STAGE_STALL_BACKOFF_MM,
    STAGE_STALL_COARSE_RETRY_COUNT,
    STAGE_STALL_COARSE_VALIDATION_COUNT,
    STAGE_STALL_FAST_APPROACH_MARGIN_MM,
    STAGE_STALL_FAST_APPROACH_MM,
    STAGE_STALL_FAST_APPROACH_RPM,
    STAGE_STALL_LOW_CONTACT_RETRY_COUNT,
    STAGE_STALL_MIN_VALID_STAGE_POSITION_MM,
    STAGE_STALL_PROBE_RPM,
    STAGE_STALL_PROBE_STEP_MM,
    STAGE_STALL_START_MM,
    STAGE_STALL_TARGET_MM,
    STAGE_STALL_TIMEOUT_S,
    STAGE_STALL_VALIDATION_COUNT,
    STAGE_STALL_VALIDATION_HIGH_SIDE_WINDOW_DEG,
    STAGE_STALL_VALIDATION_MIN_VALID_SAMPLE_COUNT,
    STAGE_STALL_VALIDATION_PLATEAU_TOLERANCE_MM,
    STAGE_STALL_VALIDATION_REPEATS,
    STAGE_STALL_VALIDATION_RETRY_COUNT,
)

from .models import *

def _write_stage_stall_angle_debug_document(
    *,
    side_name: str,
    previous_rotation_deg: float,
    rotation_center_deg: float,
    capture_y_mm: float,
    probe_x_mm: float,
    fast_approach_mm: float,
    min_valid_stage_position_mm: float,
    validation_plateau_tolerance_mm: float,
    samples: list[Any],
    coarse_summaries: list[dict[str, Any]],
    fine_summaries: list[dict[str, Any]],
    validation_summaries: list[dict[str, Any]],
    selected_summaries: list[dict[str, Any]],
    best_summary: dict[str, Any] | None,
    selected_pass: str,
    rotation_positions: list[float],
) -> Path:
    timestamp = _get_timestamp().replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    output_path = Path(_build_default_scans_dir()) / ANGLE_DEBUG_DIR_NAME / f"{side_name}-stage-stall-{timestamp}.json"
    angle_module.write_stage_stall_output_document(
        output_path=output_path,
        metadata={
            "side": side_name,
            "previous_rotation_deg": float(previous_rotation_deg),
            "rotation_center_deg": float(rotation_center_deg),
            "capture_y_mm": float(capture_y_mm),
            "probe_x_mm": float(probe_x_mm),
            "fast_approach_mm": float(fast_approach_mm),
            "min_valid_stage_position_mm": float(min_valid_stage_position_mm),
            "validation_plateau_tolerance_mm": float(validation_plateau_tolerance_mm),
            "coarse_rotation_positions": rotation_positions,
            "coarse_summaries": coarse_summaries,
            "fine_summaries": fine_summaries,
            "validation_summaries": validation_summaries,
            "selected_pass": selected_pass,
        },
        samples=samples,
        angle_summaries=selected_summaries,
        best_summary=best_summary,
    )
    return output_path


def _write_shelf_scout_angle_debug_document(
    *,
    side_name: str,
    previous_rotation_deg: float,
    rotation_center_deg: float,
    probe_x_mm: float,
    error: Any,
) -> Path:
    timestamp = _get_timestamp().replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    output_path = Path(_build_default_scans_dir()) / ANGLE_DEBUG_DIR_NAME / f"{side_name}-shelf-scout-{timestamp}.json"
    debug_document = dict(error.debug_document)
    metadata = dict(debug_document.get("metadata", {}))
    metadata.update(
        {
            "side": side_name,
            "previous_rotation_deg": float(previous_rotation_deg),
            "rotation_center_deg": float(rotation_center_deg),
            "probe_x_mm": float(probe_x_mm),
            "error": str(error),
        }
    )
    debug_document["metadata"] = metadata
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(debug_document, indent="\t"), encoding="utf-8")
    return output_path


def _get_stage_stall_summary_median(summary: dict[str, Any]) -> float | None:
    median_stage_position_mm = summary.get("median_stage_position_mm")

    if median_stage_position_mm is None:
        return None

    return float(median_stage_position_mm)


def _build_stage_stall_validation_rotation_positions(
    *,
    fine_summaries: list[dict[str, Any]],
    coarse_summaries: list[dict[str, Any]],
    validation_count: int,
    validation_repeats: int,
    coarse_validation_count: int,
    high_side_window_deg: float,
) -> list[float]:
    if validation_count <= 0 or validation_repeats <= 0:
        return []

    ordered_fine_summaries = [
        summary
        for summary in fine_summaries
        if _get_stage_stall_summary_median(summary) is not None
    ]
    ordered_fine_summaries.sort(
        key=lambda summary: float(_get_stage_stall_summary_median(summary) or 0.0),
        reverse=True,
    )
    ordered_coarse_summaries = [
        summary
        for summary in coarse_summaries
        if _get_stage_stall_summary_median(summary) is not None
    ]
    ordered_coarse_summaries.sort(
        key=lambda summary: float(_get_stage_stall_summary_median(summary) or 0.0),
        reverse=True,
    )
    candidate_rotations: list[float] = []

    def did_append_candidate(rotation_deg: float) -> None:
        if any(abs(rotation_deg - candidate) < 1e-9 for candidate in candidate_rotations):
            return

        candidate_rotations.append(rotation_deg)

    for summary in ordered_fine_summaries[:validation_count]:
        did_append_candidate(float(summary["rotation_deg"]))

    if len(ordered_fine_summaries) > 0 and high_side_window_deg > 0.0:
        best_fine_rotation_deg = float(ordered_fine_summaries[0]["rotation_deg"])
        high_side_summaries = sorted(
            fine_summaries,
            key=lambda summary: float(summary["rotation_deg"]),
        )

        for summary in high_side_summaries:
            rotation_deg = float(summary["rotation_deg"])

            if rotation_deg < best_fine_rotation_deg:
                continue

            if rotation_deg > best_fine_rotation_deg + high_side_window_deg:
                continue

            did_append_candidate(rotation_deg)

    for summary in ordered_coarse_summaries[:coarse_validation_count]:
        did_append_candidate(float(summary["rotation_deg"]))

    validation_rotation_positions: list[float] = []

    for rotation_deg in candidate_rotations:
        validation_rotation_positions.extend([rotation_deg] * validation_repeats)

    return validation_rotation_positions


def _summarize_stage_stall_samples(
    samples: list[Any],
    *,
    min_stage_position_mm: float,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    summaries, best_summary = angle_module.summarize_stage_stall_samples(samples)
    valid_counts: dict[float, int] = {}

    for sample in samples:
        if sample.stalled is not True:
            continue

        if float(sample.stage_position_mm) < min_stage_position_mm:
            continue

        rotation_deg = float(sample.rotation_deg)
        valid_counts[rotation_deg] = valid_counts.get(rotation_deg, 0) + 1

    for summary in summaries:
        summary["valid_stall_count"] = valid_counts.get(float(summary["rotation_deg"]), 0)

    if best_summary is not None:
        best_summary["valid_stall_count"] = valid_counts.get(float(best_summary["rotation_deg"]), 0)

    return summaries, best_summary


def _select_stage_stall_best_summary(
    summaries: list[dict[str, Any]],
    *,
    min_stage_position_mm: float,
    plateau_tolerance_mm: float,
    min_valid_sample_count: int = 1,
) -> dict[str, Any] | None:
    valid_summaries = [
        summary
        for summary in summaries
        if (median_stage_position_mm := _get_stage_stall_summary_median(summary)) is not None
        and median_stage_position_mm >= min_stage_position_mm
        and int(summary.get("valid_stall_count", summary.get("stall_count", 0))) >= min_valid_sample_count
    ]

    if len(valid_summaries) == 0:
        return None

    best_median_stage_position_mm = max(
        float(_get_stage_stall_summary_median(summary) or 0.0)
        for summary in valid_summaries
    )
    plateau_summaries = [
        summary
        for summary in valid_summaries
        if best_median_stage_position_mm - float(_get_stage_stall_summary_median(summary) or 0.0)
        <= max(plateau_tolerance_mm, 0.0)
    ]

    return max(plateau_summaries, key=lambda summary: float(summary["rotation_deg"]))



class AngleRunnerMixin:
    async def _run_angle_calibration_for_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
    ) -> LaserAutoCalibrationAngleResult:
        return await self._run_stage_stall_angle_calibration_for_side(worker, side, bounds)

    async def _run_stage_stall_angle_calibration_for_side(
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

        stage_motor = tool._ms
        rotation_motor = tool._mr

        side_name = _side_to_short_name(side)
        side_module_name = _side_to_module_name(side)
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        rotation_param = _side_to_rotation_param_name(side)
        previous_rotation_value = await tool.get_param(rotation_param)

        if previous_rotation_value is None:
            raise ValueError(f"{rotation_param} is required")

        previous_rotation_deg = float(previous_rotation_value)
        rotation_center_deg = previous_rotation_deg
        rotation_limit_deg = float(rotation_motor._params.lim)
        rotation_positions = _build_stage_stall_rotation_positions(
            scan_config,
            side,
            rotation_center_deg,
            rotation_limit_deg,
        )
        settle_ms = int(laser_module.get_laser_sample_settle_ms())
        samples_per_point = int(laser_module.get_laser_samples_per_point())
        laser_scan_pose_z_delta_from_place_mm = laser_module.get_laser_scan_pose_z_delta_from_place_mm(scan_config)
        stage_extension_mm = laser_module.get_laser_scan_pose_stage_mm(scan_config)
        z_extension_place = await tool.get_param("Z_EXTENSION_PLACE")
        z_move_rpm = await tool.get_param("Z_MOVE_RPM")
        z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

        if z_extension_place is None or z_move_rpm is None or z_move_accel is None:
            raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required")

        base_z_mm = float(z_extension_place) + laser_scan_pose_z_delta_from_place_mm
        probe_x_mm = _resolve_stage_stall_probe_x_mm(scan_config)
        stage_limit_mm = float(stage_motor._params.lim)
        probe_start_mm = float(STAGE_STALL_START_MM)
        probe_target_mm = float(
            STAGE_STALL_TARGET_MM if STAGE_STALL_TARGET_MM is not None else stage_limit_mm
        )
        probe_step_mm = float(STAGE_STALL_PROBE_STEP_MM)
        probe_rpm = int(STAGE_STALL_PROBE_RPM)
        fast_approach_rpm = int(STAGE_STALL_FAST_APPROACH_RPM)
        fast_approach_mm = angle_module.resolve_stage_stall_fast_approach_mm(
            requested_fast_approach_mm=STAGE_STALL_FAST_APPROACH_MM,
            parameters=worker._robot._tool._params.parameters,
            side_name=side_name,
            probe_start_mm=probe_start_mm,
            probe_target_mm=probe_target_mm,
            margin_mm=float(STAGE_STALL_FAST_APPROACH_MARGIN_MM),
        )
        stage_backoff_mm = float(STAGE_STALL_BACKOFF_MM)
        stage_timeout_s = float(STAGE_STALL_TIMEOUT_S)
        no_progress_timeout_s = float(getattr(scan_config, "angle_calibration_stage_stall_no_progress_timeout_s", 0.25))
        min_valid_stage_position_mm = float(STAGE_STALL_MIN_VALID_STAGE_POSITION_MM)
        validation_retry_count = int(STAGE_STALL_VALIDATION_RETRY_COUNT)
        coarse_retry_count = int(STAGE_STALL_COARSE_RETRY_COUNT)
        validation_plateau_tolerance_mm = float(STAGE_STALL_VALIDATION_PLATEAU_TOLERANCE_MM)
        coarse_validation_count = int(STAGE_STALL_COARSE_VALIDATION_COUNT)
        validation_high_side_window_deg = float(STAGE_STALL_VALIDATION_HIGH_SIDE_WINDOW_DEG)
        low_contact_retry_count = int(STAGE_STALL_LOW_CONTACT_RETRY_COUNT)
        validation_min_valid_sample_count = int(STAGE_STALL_VALIDATION_MIN_VALID_SAMPLE_COUNT)
        angle_args = argparse.Namespace(
            y_mm=None,
            y_range=(bounds.y_min, bounds.y_max),
            scout_x_mm=None,
        )
        samples: list[angle_module.StageStallProbeSample] = []
        coarse_samples: list[angle_module.StageStallProbeSample] = []
        fine_samples: list[angle_module.StageStallProbeSample] = []
        validation_samples: list[angle_module.StageStallProbeSample] = []
        capture_y_mm = float("nan")

        async with self._patch_status_callbacks(
            scan_callback=None,
            angle_callback=self._build_angle_status_callback(side_module_name),
        ):
            try:
                self._raise_if_abort_requested()
                await tool.retract()
                try:
                    capture_y_mm = await angle_module.resolve_capture_y_mm(
                        tool,
                        args=angle_args,
                        scan_config=scan_config,
                        side_name=side_name,
                        side_number=side_number,
                        x_positions=[probe_x_mm],
                        rotation_center_deg=rotation_center_deg,
                        stage_extension_mm=stage_extension_mm,
                        base_z_mm=base_z_mm,
                        z_move_rpm=float(z_move_rpm),
                        z_move_accel=float(z_move_accel),
                        settle_ms=settle_ms,
                        samples_per_point=samples_per_point,
                        should_abort=self._should_abort_work,
                    )
                except angle_module.ShelfScoutError as exc:
                    try:
                        debug_path = _write_shelf_scout_angle_debug_document(
                            side_name=side_module_name,
                            previous_rotation_deg=previous_rotation_deg,
                            rotation_center_deg=rotation_center_deg,
                            probe_x_mm=probe_x_mm,
                            error=exc,
                        )
                        angle_module.print_status(f"shelf_scout=debug path={debug_path}")
                    except Exception as debug_exc:
                        logger.warning("Failed to write shelf-scout angle debug document: %s", debug_exc)

                    raise
                current_probe_start_mm = probe_start_mm

                async def did_run_stage_stall_pass(
                    *,
                    pass_name: str,
                    pass_rotation_positions: list[float],
                ) -> list[angle_module.StageStallProbeSample]:
                    nonlocal current_probe_start_mm
                    pass_samples: list[angle_module.StageStallProbeSample] = []

                    for rotation_idx, rotation_deg in enumerate(pass_rotation_positions, start=1):
                        for low_contact_attempt_idx in range(low_contact_retry_count + 1):
                            self._raise_if_abort_requested()
                            absolute_rotation_deg = rotation_center_deg + float(rotation_deg)
                            angle_module.print_status(
                                f"phase=stage-stall pass={pass_name} "
                                f"rotation={rotation_idx}/{len(pass_rotation_positions)} "
                                f"attempt={low_contact_attempt_idx + 1}/{low_contact_retry_count + 1} "
                                f"absolute_rotation_deg={absolute_rotation_deg:.3f}"
                            )
                            await angle_module.prepare_capture_pose(
                                tool,
                                side_name=side_name,
                                side_number=side_number,
                                x_mm=probe_x_mm,
                                y_mm=capture_y_mm,
                                absolute_rotation_deg=absolute_rotation_deg,
                                stage_extension_mm=current_probe_start_mm,
                                z_mm=base_z_mm,
                                z_move_rpm=float(z_move_rpm),
                                z_move_accel=float(z_move_accel),
                                should_rotate=True,
                                should_use_safe_right_rotation_path=True,
                                should_move_z=False,
                                should_abort=self._should_abort_work,
                            )
                            self._raise_if_abort_requested()
                            did_stall, stage_position_mm, status, alarm_code = await angle_module.probe_stage_until_stall(
                                tool,
                                target_stage_mm=probe_target_mm,
                                retract_stage_mm=current_probe_start_mm,
                                settle_ms=settle_ms,
                                timeout_s=stage_timeout_s,
                                probe_rpm=probe_rpm,
                                no_progress_s=no_progress_timeout_s,
                                probe_step_mm=probe_step_mm,
                                fast_approach_mm=fast_approach_mm,
                                fast_approach_rpm=fast_approach_rpm,
                                auto_retract=False,
                                should_use_continuous_move=True,
                                should_abort=self._should_abort_work,
                            )
                            sample = angle_module.StageStallProbeSample(
                                rotation_deg=float(rotation_deg),
                                absolute_rotation_deg=absolute_rotation_deg,
                                probe_x_mm=probe_x_mm,
                                probe_y_mm=capture_y_mm,
                                stage_position_mm=stage_position_mm,
                                stalled=did_stall,
                                status=status,
                                alarm_code=None if alarm_code is None else int(alarm_code),
                                pass_name=pass_name,
                            )
                            samples.append(sample)
                            pass_samples.append(sample)
                            if did_stall is True:
                                alarm_label = "none" if alarm_code is None else str(int(alarm_code))
                                angle_module.print_status(
                                    f"stage_probe=stall pass={pass_name} "
                                    f"rotation_deg={float(rotation_deg):.3f} "
                                    f"absolute_rotation_deg={absolute_rotation_deg:.3f} "
                                    f"status={status} "
                                    f"stage_position_mm={stage_position_mm:.3f} "
                                    f"alarm_code={alarm_label}"
                                )
                            else:
                                angle_module.print_status(
                                    f"stage_probe=skip pass={pass_name} "
                                    f"rotation_deg={float(rotation_deg):.3f} "
                                    f"absolute_rotation_deg={absolute_rotation_deg:.3f} "
                                    f"status={status} stage_position_mm={stage_position_mm:.3f}"
                                )

                            stage_retract_target_mm = angle_module.resolve_stage_retract_target(
                                did_stall=did_stall,
                                stage_position_mm=stage_position_mm,
                                probe_start_mm=probe_start_mm,
                                stage_backoff_mm=stage_backoff_mm,
                            )
                            angle_module.print_status(
                                f"stage_probe=retract pass={pass_name} target_mm={stage_retract_target_mm:.3f}"
                            )
                            await angle_module.retract_stage_after_stall(
                                tool,
                                retract_stage_mm=stage_retract_target_mm,
                                probe_rpm=probe_rpm,
                            )
                            current_probe_start_mm = stage_retract_target_mm

                            should_retry_low_contact = (
                                did_stall is True
                                and float(stage_position_mm) < min_valid_stage_position_mm
                                and low_contact_attempt_idx < low_contact_retry_count
                            )

                            if should_retry_low_contact is False:
                                break

                            angle_module.print_status(
                                "stage_probe=low_contact_retry "
                                f"pass={pass_name} "
                                f"rotation_deg={float(rotation_deg):.3f} "
                                f"stage_position_mm={stage_position_mm:.3f} "
                                f"min_stage_position_mm={min_valid_stage_position_mm:.3f}"
                            )
                            await stage_motor.home()
                            current_probe_start_mm = probe_start_mm

                        if rotation_idx < len(pass_rotation_positions):
                            angle_module.print_status(f"stage_probe=home_after_retract pass={pass_name}")
                            await stage_motor.home()
                            current_probe_start_mm = probe_start_mm

                    return pass_samples

                coarse_samples = []

                for coarse_attempt_idx in range(coarse_retry_count + 1):
                    pass_name = "coarse" if coarse_attempt_idx == 0 else "coarse_retry"
                    coarse_samples = await did_run_stage_stall_pass(
                        pass_name=pass_name,
                        pass_rotation_positions=rotation_positions,
                    )
                    _coarse_summaries, _raw_coarse_best_summary = _summarize_stage_stall_samples(
                        coarse_samples,
                        min_stage_position_mm=min_valid_stage_position_mm,
                    )
                    coarse_best_summary = _select_stage_stall_best_summary(
                        _coarse_summaries,
                        min_stage_position_mm=min_valid_stage_position_mm,
                        plateau_tolerance_mm=validation_plateau_tolerance_mm,
                    )

                    if coarse_best_summary is not None:
                        break

                    angle_module.print_status(
                        "stage_probe=coarse_reject "
                        f"attempt={coarse_attempt_idx + 1}/{coarse_retry_count + 1} "
                        f"min_stage_position_mm={min_valid_stage_position_mm:.3f}"
                    )

                    if coarse_attempt_idx < coarse_retry_count:
                        angle_module.print_status("stage_probe=home_before_coarse_retry")
                        await stage_motor.home()
                        current_probe_start_mm = probe_start_mm

                if coarse_best_summary is not None:
                    fine_rotation_positions = _build_stage_stall_fine_rotation_positions(
                        scan_config,
                        float(coarse_best_summary["rotation_deg"]),
                        rotation_center_deg,
                        rotation_limit_deg,
                    )
                    angle_module.print_status(
                        "stage_probe=fine_window "
                        f"coarse_best_rotation_deg={float(coarse_best_summary['rotation_deg']):.3f} "
                        f"positions={len(fine_rotation_positions)}"
                    )

                    if len(fine_rotation_positions) > 0:
                        angle_module.print_status("stage_probe=home_before_fine")
                        await stage_motor.home()
                        current_probe_start_mm = probe_start_mm
                        fine_samples = await did_run_stage_stall_pass(
                            pass_name="fine",
                            pass_rotation_positions=fine_rotation_positions,
                        )

                    fine_summaries, fine_best_summary = _summarize_stage_stall_samples(
                        fine_samples,
                        min_stage_position_mm=min_valid_stage_position_mm,
                    )

                    if fine_best_summary is not None:
                        validation_count = int(STAGE_STALL_VALIDATION_COUNT)
                        validation_repeats = int(STAGE_STALL_VALIDATION_REPEATS)
                        validation_rotation_positions = _build_stage_stall_validation_rotation_positions(
                            fine_summaries=fine_summaries,
                            coarse_summaries=_coarse_summaries,
                            validation_count=validation_count,
                            validation_repeats=validation_repeats,
                            coarse_validation_count=coarse_validation_count,
                            high_side_window_deg=validation_high_side_window_deg,
                        )

                        if len(validation_rotation_positions) > 0:
                            unique_validation_angles = len(set(validation_rotation_positions))
                            angle_module.print_status(
                                "stage_probe=validation_window "
                                f"positions={len(validation_rotation_positions)} "
                                f"unique_angles={unique_validation_angles} "
                                f"repeats={validation_repeats}"
                            )

                            for validation_attempt_idx in range(validation_retry_count + 1):
                                pass_name = "validation" if validation_attempt_idx == 0 else "validation_retry"
                                angle_module.print_status(f"stage_probe=home_before_{pass_name}")
                                await stage_motor.home()
                                current_probe_start_mm = probe_start_mm
                                attempt_samples = await did_run_stage_stall_pass(
                                    pass_name=pass_name,
                                    pass_rotation_positions=validation_rotation_positions,
                                )
                                attempt_summaries, _attempt_best_summary = _summarize_stage_stall_samples(
                                    attempt_samples,
                                    min_stage_position_mm=min_valid_stage_position_mm,
                                )
                                attempt_best_summary = _select_stage_stall_best_summary(
                                    attempt_summaries,
                                    min_stage_position_mm=min_valid_stage_position_mm,
                                    plateau_tolerance_mm=validation_plateau_tolerance_mm,
                                    min_valid_sample_count=validation_min_valid_sample_count,
                                )
                                validation_samples = attempt_samples

                                if attempt_best_summary is not None:
                                    break

                                angle_module.print_status(
                                    "stage_probe=validation_reject "
                                    f"attempt={validation_attempt_idx + 1}/{validation_retry_count + 1} "
                                    f"min_stage_position_mm={min_valid_stage_position_mm:.3f}"
                                )
            finally:
                if _should_skip_motion_cleanup_on_cancel() is False:
                    await angle_module.finish_stage_stall_capture(
                        tool,
                        z_move_rpm=float(z_move_rpm),
                        z_move_accel=float(z_move_accel),
                    )

        validation_summaries, _validation_best_summary = _summarize_stage_stall_samples(
            validation_samples,
            min_stage_position_mm=min_valid_stage_position_mm,
        )
        fine_summaries, _fine_best_summary = _summarize_stage_stall_samples(
            fine_samples,
            min_stage_position_mm=min_valid_stage_position_mm,
        )
        coarse_summaries, _coarse_best_summary = _summarize_stage_stall_samples(
            coarse_samples,
            min_stage_position_mm=min_valid_stage_position_mm,
        )
        validation_best_summary = _select_stage_stall_best_summary(
            validation_summaries,
            min_stage_position_mm=min_valid_stage_position_mm,
            plateau_tolerance_mm=validation_plateau_tolerance_mm,
            min_valid_sample_count=validation_min_valid_sample_count,
        )
        fine_best_summary = _select_stage_stall_best_summary(
            fine_summaries,
            min_stage_position_mm=min_valid_stage_position_mm,
            plateau_tolerance_mm=validation_plateau_tolerance_mm,
        )
        coarse_best_summary = _select_stage_stall_best_summary(
            coarse_summaries,
            min_stage_position_mm=min_valid_stage_position_mm,
            plateau_tolerance_mm=validation_plateau_tolerance_mm,
        )
        selected_pass = "validation"
        selected_summaries = validation_summaries
        best_summary = validation_best_summary
        validation_reject_error: str | None = None

        if len(validation_samples) > 0 and validation_best_summary is None:
            selected_pass = "validation_rejected"
            selected_summaries = validation_summaries
            best_summary = fine_best_summary if fine_best_summary is not None else coarse_best_summary
            validation_reject_error = (
                "stage-stall validation did not reach minimum contact depth "
                f"{min_valid_stage_position_mm:.3f} mm"
            )

        if best_summary is None:
            selected_pass = "fine"
            selected_summaries = fine_summaries
            best_summary = fine_best_summary

        if best_summary is None:
            selected_pass = "coarse"
            selected_summaries = coarse_summaries
            best_summary = coarse_best_summary

        if best_summary is None:
            try:
                debug_path = _write_stage_stall_angle_debug_document(
                    side_name=side_module_name,
                    previous_rotation_deg=previous_rotation_deg,
                    rotation_center_deg=rotation_center_deg,
                    capture_y_mm=capture_y_mm,
                    probe_x_mm=probe_x_mm,
                    fast_approach_mm=fast_approach_mm,
                    min_valid_stage_position_mm=min_valid_stage_position_mm,
                    validation_plateau_tolerance_mm=validation_plateau_tolerance_mm,
                    samples=samples,
                    coarse_summaries=coarse_summaries,
                    fine_summaries=fine_summaries,
                    validation_summaries=validation_summaries,
                    selected_summaries=selected_summaries,
                    best_summary=None,
                    selected_pass="invalid",
                    rotation_positions=rotation_positions,
                )
                angle_module.print_status(f"stage_probe=debug path={debug_path}")
            except Exception as exc:
                logger.warning("Failed to write stage-stall angle debug document: %s", exc)

            raise ValueError("stage-stall angle calibration did not detect any valid stage contacts")

        updated_rotation_deg = float(best_summary["absolute_rotation_deg"])
        alpha_deg = updated_rotation_deg - rotation_center_deg

        if updated_rotation_deg < 0.0 or updated_rotation_deg > rotation_limit_deg:
            raise ValueError(
                "stage-stall rotation is outside motor range "
                f"({updated_rotation_deg:.3f} deg is outside 0..{rotation_limit_deg:.3f})"
            )

        try:
            debug_path = _write_stage_stall_angle_debug_document(
                side_name=side_module_name,
                previous_rotation_deg=previous_rotation_deg,
                rotation_center_deg=rotation_center_deg,
                capture_y_mm=capture_y_mm,
                probe_x_mm=probe_x_mm,
                fast_approach_mm=fast_approach_mm,
                min_valid_stage_position_mm=min_valid_stage_position_mm,
                validation_plateau_tolerance_mm=validation_plateau_tolerance_mm,
                samples=samples,
                coarse_summaries=coarse_summaries,
                fine_summaries=fine_summaries,
                validation_summaries=validation_summaries,
                selected_summaries=selected_summaries,
                best_summary=best_summary,
                selected_pass=selected_pass,
                rotation_positions=rotation_positions,
            )
            angle_module.print_status(f"stage_probe=debug path={debug_path}")
        except Exception as exc:
            logger.warning("Failed to write stage-stall angle debug document: %s", exc)

        if validation_reject_error is not None:
            raise ValueError(validation_reject_error)

        return LaserAutoCalibrationAngleResult(
            method="stage_stall",
            side=side,
            alpha_deg=alpha_deg,
            sample_count=len(samples),
            capture_y_mm=float(capture_y_mm),
            rms_residual_mm=0.0,
            max_abs_residual_mm=0.0,
            line_angle_deg=0.0,
            previous_rotation_deg=previous_rotation_deg,
            updated_rotation_deg=updated_rotation_deg,
            stage_position_mm=float(best_summary["median_stage_position_mm"]),
        )


    async def _apply_angle_calibration(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        result: LaserAutoCalibrationAngleResult,
    ) -> None:
        rotation_param = _side_to_rotation_param_name(side)
        self._persist_tool_parameter(worker, rotation_param, result.updated_rotation_deg)
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

