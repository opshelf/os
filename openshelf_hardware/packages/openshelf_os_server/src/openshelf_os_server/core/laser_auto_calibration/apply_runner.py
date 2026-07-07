# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

from pathlib import Path

from openshelf_os.laser_calibration.angle.capture import enter_capture_mode
from openshelf_os.laser_calibration.angle.robot_ops import probe_z_until_stall

from .models import *


_MANUAL_SHELF_MATCH_TOLERANCE_MM = 60.0
_MANUAL_CELL_MATCH_TOLERANCE_MM = 40.0


def _shelf_center_y(shelf: Any) -> float | None:
    ys = [float(cell.calibration_point.y) for cell in shelf.cells if cell.calibration_point is not None]

    if len(ys) == 0:
        return None

    return sum(ys) / len(ys)


def _preserve_manual_cells(scanned_shelves: list[Any], existing_shelves: list[Any]) -> list[Any]:
    manual_groups: list[tuple[Any, list[Any]]] = []
    for shelf in existing_shelves:
        manual_cells = [
            cell for cell in shelf.cells
            if getattr(cell, "is_manual", False) is True and cell.calibration_point is not None
        ]
        if len(manual_cells) > 0:
            manual_groups.append((shelf, manual_cells))

    if len(manual_groups) == 0:
        return scanned_shelves

    result = list(scanned_shelves)
    modified_shelf_ids: set[int] = set()

    for source_shelf, manual_cells in manual_groups:
        source_y = _shelf_center_y(source_shelf)
        target = None

        if source_y is not None:
            within = [
                (abs((_shelf_center_y(shelf) or 1e9) - source_y), shelf)
                for shelf in result
                if _shelf_center_y(shelf) is not None
            ]
            within = [pair for pair in within if pair[0] <= _MANUAL_SHELF_MATCH_TOLERANCE_MM]
            if len(within) > 0:
                target = min(within, key=lambda pair: pair[0])[1]

        if target is None:
            preserved = source_shelf.model_copy(deep=True)
            preserved.cells = [cell.model_copy(deep=True) for cell in manual_cells]
            result.append(preserved)
            modified_shelf_ids.add(id(preserved))
            continue

        for manual_cell in manual_cells:
            manual_x = float(manual_cell.calibration_point.x)
            target.cells = [
                cell for cell in target.cells
                if cell.calibration_point is None
                or abs(float(cell.calibration_point.x) - manual_x) > _MANUAL_CELL_MATCH_TOLERANCE_MM
            ]
            target.cells.append(manual_cell.model_copy(deep=True))
        modified_shelf_ids.add(id(target))

    for shelf in result:
        if id(shelf) not in modified_shelf_ids:
            continue
        calibrated = sorted(
            [cell for cell in shelf.cells if cell.calibration_point is not None],
            key=lambda cell: float(cell.calibration_point.x),
        )
        uncalibrated = [cell for cell in shelf.cells if cell.calibration_point is None]
        for idx, cell in enumerate(calibrated):
            cell.cell_idx = idx
        shelf.cells = calibrated + uncalibrated

    return result


def _select_z_stall_probes(analysis: Any, candidate_lookup: dict[tuple[int, int], Any]) -> list[tuple[Any, float]]:
    shelves = [shelf for shelf in analysis.shelves if len(shelf.cells) > 0]

    if len(shelves) == 0:
        return []

    ordered_shelves = sorted(shelves, key=lambda shelf: float(shelf.center_y_mm))
    middle = len(ordered_shelves) // 2
    shelf_sequence: list[Any] = [ordered_shelves[middle]]
    for step in range(1, len(ordered_shelves)):
        if middle + step < len(ordered_shelves):
            shelf_sequence.append(ordered_shelves[middle + step])
        if middle - step >= 0:
            shelf_sequence.append(ordered_shelves[middle - step])

    probes: list[tuple[Any, float]] = []
    seen_cells: set[tuple[int, int]] = set()
    for shelf in shelf_sequence:
        valid_cells = [
            (cell, float(candidate_lookup[(cell.shelf_idx, cell.cell_idx)].wall_depth_mm))
            for cell in sorted(shelf.cells, key=lambda cell: float(cell.center_x_mm))
            if candidate_lookup.get((cell.shelf_idx, cell.cell_idx)) is not None
            and candidate_lookup[(cell.shelf_idx, cell.cell_idx)].wall_depth_mm is not None
            and float(candidate_lookup[(cell.shelf_idx, cell.cell_idx)].wall_depth_mm) > 0.0
        ]

        if len(valid_cells) == 0:
            continue

        # Probe both X extremes of each shelf so a left/right rack tilt can be fitted;
        # the middle shelf comes first, so the two ends of the best shelf are tried first.
        for cell, wall_depth_mm in (valid_cells[0], valid_cells[-1]):
            cell_key = (cell.shelf_idx, cell.cell_idx)
            if cell_key in seen_cells:
                continue
            seen_cells.add(cell_key)
            probes.append((cell, wall_depth_mm))

    return probes


class ApplyRunnerMixin:
    async def _run_z_stall_anchor(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
    ) -> tuple[float, float]:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        tool = worker._robot._tool

        xy_offset = laser_module.get_scan_xy_offset(scan_config=scan_config, side=side)
        offset_x_mm = 0.0 if xy_offset is None else float(xy_offset[0])

        metadata = laser_module.load_json_file(Path(str(analysis.metadata_path)))
        candidates = laser_module.load_candidates_from_metadata(metadata)
        candidate_lookup = laser_module.build_candidate_lookup_by_cell(analysis=analysis, candidates=candidates)

        probes = _select_z_stall_probes(analysis, candidate_lookup)
        if len(probes) == 0:
            raise ValueError("z stall anchor: no detected cell with a wall depth to probe")

        side_name = "left" if side == StorageModuleSide.LEFT else "right"
        side_number = 0 if side == StorageModuleSide.LEFT else 1
        rotation_param = "ROTATION_FRONT" if side_name == "left" else "ROTATION_BACK"
        stage_param = "STAGE_EXTENSION_SHELF_FRONT" if side_name == "left" else "STAGE_EXTENSION_SHELF_BACK"

        rotation_center_value = await tool.get_param(rotation_param)
        stage_extension_value = await tool.get_param(stage_param)
        z_place = await tool.get_param("Z_EXTENSION_PLACE")
        z_move_rpm = await tool.get_param("Z_MOVE_RPM")
        z_move_accel = await tool.get_param("Z_MOVE_ACCEL")

        if rotation_center_value is None or stage_extension_value is None:
            raise ValueError(f"{rotation_param} and {stage_param} are required for z stall anchor")

        if z_place is None or z_move_rpm is None or z_move_accel is None:
            raise ValueError("Z_EXTENSION_PLACE, Z_MOVE_RPM, and Z_MOVE_ACCEL are required for z stall anchor")

        rotation_center_deg = float(rotation_center_value)
        stage_extension_mm = float(stage_extension_value)
        probe_rpm = scan_config.z_stall_probe_rpm
        laser_module.print_status(
            f"z_stall_anchor=start side={side_name} rotation_deg={rotation_center_deg:.3f} "
            f"stage_extension_mm={stage_extension_mm:.2f} candidate_cells={len(probes)}"
        )

        samples: list[tuple[int, float, float]] = []
        last_reason = "none"
        for detected_cell, probe_wall_depth_mm in probes:
            probe_x_mm = float(detected_cell.tool_x_mm) + float(scan_config.z_stall_probe_x_shift_mm)
            probe_y_mm = float(detected_cell.tool_y_mm)

            await enter_capture_mode(
                tool,
                side_name=side_name,
                side_number=side_number,
                x_mm=probe_x_mm,
                y_mm=probe_y_mm,
                absolute_rotation_deg=rotation_center_deg,
                stage_extension_mm=stage_extension_mm,
                z_mm=float(scan_config.z_stall_probe_start_mm),
                z_move_rpm=float(z_move_rpm),
                z_move_accel=float(z_move_accel),
                should_abort=self._should_abort_work,
            )

            did_stall, z_contact_mm, reason, alarm_code = await probe_z_until_stall(
                tool,
                target_z_mm=float(scan_config.z_stall_probe_target_mm),
                retract_z_mm=float(scan_config.z_stall_probe_start_mm),
                settle_ms=laser_module.get_laser_sample_settle_ms(scan_config),
                timeout_s=float(scan_config.z_stall_probe_timeout_s),
                probe_rpm=None if probe_rpm is None else int(probe_rpm),
                no_progress_s=float(scan_config.z_stall_probe_no_progress_s),
                probe_step_mm=float(scan_config.z_stall_probe_step_mm),
                fast_approach_mm=float(scan_config.z_stall_probe_fast_approach_mm),
                fast_approach_rpm=None if probe_rpm is None else int(probe_rpm),
                auto_retract=True,
                should_abort=self._should_abort_work,
            )
            last_reason = reason

            if did_stall is False:
                laser_module.print_status(
                    f"z_stall_anchor=miss side={side_name} probe={detected_cell.shelf_idx}:{detected_cell.cell_idx} "
                    f"reason={reason} z_mm={z_contact_mm:.3f}"
                )
                continue

            z_offset_anchor_mm = z_contact_mm - float(z_place) + float(scan_config.z_stall_tray_engagement_mm)
            z_delta_mm = z_offset_anchor_mm - probe_wall_depth_mm
            sample_x_mm = float(detected_cell.tool_x_mm) + offset_x_mm
            samples.append((detected_cell.shelf_idx, sample_x_mm, z_delta_mm))
            laser_module.print_status(
                f"z_stall_anchor=sample side={side_name} probe={detected_cell.shelf_idx}:{detected_cell.cell_idx} "
                f"grab_x_mm={sample_x_mm:.3f} z_contact_mm={z_contact_mm:.3f} z_place_mm={float(z_place):.3f} "
                f"tray_mm={float(scan_config.z_stall_tray_engagement_mm):.3f} "
                f"probe_wall_depth_mm={probe_wall_depth_mm:.3f} anchor_z_offset_mm={z_offset_anchor_mm:.3f} "
                f"delta_mm={z_delta_mm:.3f}"
            )

            # A trustworthy X slope must come from a single shelf (same Y); break as soon
            # as one shelf yields a wide enough X pair, so we do not over-probe.
            shelf_sample_xs = [x for shelf_idx, x, _ in samples if shelf_idx == detected_cell.shelf_idx]
            if len(shelf_sample_xs) >= 2 and (max(shelf_sample_xs) - min(shelf_sample_xs)) >= laser_module.Z_DELTA_TILT_MIN_X_SPAN_MM:
                break

        if len(samples) == 0:
            raise ValueError(
                f"z stall anchor: no probe cell contacted a wall across {len(probes)} shelves "
                f"(last reason={last_reason}, stage_extension_mm={stage_extension_mm:.2f})"
            )

        # Fit the X slope only from the single shelf that gives the widest X pair, so
        # vertical/inter-shelf Z differences never leak into the tilt; otherwise fall
        # back to a constant delta averaged over every successful probe.
        samples_by_shelf: dict[int, list[tuple[float, float]]] = {}
        for shelf_idx, sample_x_mm, sample_delta_mm in samples:
            samples_by_shelf.setdefault(shelf_idx, []).append((sample_x_mm, sample_delta_mm))

        def _sample_x_span(shelf_samples: list[tuple[float, float]]) -> float:
            shelf_xs = [x for x, _ in shelf_samples]
            return max(shelf_xs) - min(shelf_xs)

        best_shelf_samples = max(samples_by_shelf.values(), key=_sample_x_span)
        fit_x_span_mm = _sample_x_span(best_shelf_samples)

        if len(best_shelf_samples) >= 2 and fit_x_span_mm >= laser_module.Z_DELTA_TILT_MIN_X_SPAN_MM:
            intercept_mm, slope_per_mm = laser_module.fit_z_delta_line(best_shelf_samples)
            fit_source = "single_shelf"
        else:
            all_deltas = [sample_delta_mm for _, _, sample_delta_mm in samples]
            intercept_mm, slope_per_mm = sum(all_deltas) / len(all_deltas), 0.0
            fit_source = "constant"

        laser_module.print_status(
            f"z_stall_anchor=fit side={side_name} source={fit_source} samples={len(samples)} "
            f"shelves={len(samples_by_shelf)} x_span_mm={fit_x_span_mm:.1f} "
            f"intercept_mm={intercept_mm:.3f} slope_per_mm={slope_per_mm:.5f}"
        )
        return intercept_mm, slope_per_mm

    async def _persist_completed_results(
        self,
        worker: RobotWorker,
        completed_results: list[_CompletedSideResult],
    ) -> None:
        await self._update_snapshot(
            state="applying",
            current_stage="apply",
            stage_detail={
                "phase": "apply",
                "side_count": len(completed_results),
            },
        )

        config = get_root_config()
        next_config = config.model_copy(deep=True)
        params_dict = next_config.robot.tool.parameters.model_dump()

        for completed_result in completed_results:
            rotation_param = _side_to_rotation_param_name(completed_result.side)
            stage_extension_param = _side_to_stage_extension_param_name(completed_result.side)
            side_state = self._side_states.get(_side_to_module_name(completed_result.side))

            if completed_result.angle_result is not None:
                params_dict[rotation_param] = completed_result.angle_result.updated_rotation_deg

            if side_state is not None and side_state.stage_extension_mm is not None:
                params_dict[stage_extension_param] = float(side_state.stage_extension_mm)

        next_config.robot.tool.parameters = Parameters.model_validate(params_dict)

        scan_config = next_config.robot.grid.scan
        grid_path = Path(resolve_grid_path(next_config))
        source_grid = laser_module.load_grid(grid_path)
        left_module = list(source_grid.left_module)
        right_module = list(source_grid.right_module)

        for completed_result in completed_results:
            if len(completed_result.analysis.shelves) == 0:
                raise ValueError("No tray openings detected from scan")

            scanned_shelves = laser_module.build_shelves_from_scan(
                side=completed_result.side,
                analysis=completed_result.analysis,
                existing_shelves=None,
            )
            laser_module.apply_xy_reference_offsets(
                scanned_shelves=scanned_shelves,
                scan_config=scan_config,
                side=completed_result.side,
            )
            z_delta_model: tuple[float, float] | None = None
            if getattr(scan_config, "z_stall_anchor_enabled", False) is True:
                z_delta_model = await self._run_z_stall_anchor(
                    worker,
                    completed_result.side,
                    completed_result.analysis,
                )
            if z_delta_model is not None:
                z_delta_intercept_mm, z_delta_slope_per_mm = z_delta_model
                laser_module.apply_wall_depth_z_offsets(
                    analysis=completed_result.analysis,
                    scanned_shelves=scanned_shelves,
                    metadata_path=str(completed_result.analysis.metadata_path),
                    z_delta_mm=z_delta_intercept_mm,
                    z_delta_slope_per_mm=z_delta_slope_per_mm,
                )

            existing_side_shelves = (
                list(source_grid.left_module)
                if completed_result.side == StorageModuleSide.LEFT
                else list(source_grid.right_module)
            )
            scanned_shelves = _preserve_manual_cells(scanned_shelves, existing_side_shelves)

            if completed_result.side == StorageModuleSide.LEFT:
                left_module = scanned_shelves
            else:
                right_module = scanned_shelves

        storage_grid = laser_module.StorageGrid(
            left_module=left_module,
            right_module=right_module,
            filename=source_grid.filename if source_grid.filename != "" else str(grid_path),
        )

        try:
            storage_grid.save()
            save_root_config(next_config)
            clear_config_cache()
            worker._robot.reload_parameters()
        except Exception:
            try:
                source_grid.save()
            except Exception:
                logger.error("laser_auto_calibration_grid_rollback_failed", exc_info=True)

            try:
                save_root_config(config)
                clear_config_cache()
                worker._robot.reload_parameters()
            except Exception:
                logger.error("laser_auto_calibration_config_rollback_failed", exc_info=True)

            raise

        worker._robot._grid = laser_module.load_grid(grid_path)

    async def _persist_single_shelf_result(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        shelf_idx: int,
    ) -> None:
        await self._update_snapshot(
            state="applying",
            current_stage="apply",
            stage_detail={
                "phase": "apply",
                "scope": "shelf",
                "side": _side_to_module_name(side),
                "shelf_idx": shelf_idx,
            },
        )

        config = get_root_config()
        scan_config = config.robot.grid.scan
        grid_path = Path(resolve_grid_path(config))
        grid = laser_module.load_grid(grid_path)
        z_delta_model: tuple[float, float] | None = None
        if getattr(scan_config, "z_stall_anchor_enabled", False) is True:
            z_delta_model = await self._run_z_stall_anchor(worker, side, analysis)
        laser_module.apply_single_shelf_analysis(
            analysis=analysis,
            grid=grid,
            grid_path=grid_path,
            scan_config=scan_config,
            side=side,
            target_shelf_idx=shelf_idx,
            z_delta_override_mm=None if z_delta_model is None else z_delta_model[0],
            z_delta_slope_per_mm=0.0 if z_delta_model is None else z_delta_model[1],
        )
        worker._robot._grid = laser_module.load_grid(grid_path)

    async def _persist_single_cell_result(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        target: laser_module.SingleCellRescanTarget,
        candidate: laser_module.GapCandidateRegion,
    ) -> None:
        await self._update_snapshot(
            state="applying",
            current_stage="apply",
            stage_detail={
                "phase": "apply",
                "scope": "cell",
                "side": _side_to_module_name(side),
                "shelf_idx": int(target.shelf_idx),
                "cell_idx": int(target.cell_idx),
            },
        )

        config = get_root_config()
        scan_config = config.robot.grid.scan
        grid_path = Path(resolve_grid_path(config))
        grid = laser_module.load_grid(grid_path)
        detected_cell = laser_module.find_detected_cell_for_candidate(
            analysis=analysis,
            candidate=candidate,
        )

        if detected_cell is None:
            raise ValueError("No detected cell matched the single-cell calibration target")

        laser_module.apply_single_cell_rescan(
            grid=grid,
            target=target,
            side=side,
            detected_cell=detected_cell,
            scan_config=scan_config,
        )
        worker._robot._grid = laser_module.load_grid(grid_path)

    async def _persist_single_cell_shelf_result(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        target: laser_module.SingleCellRescanTarget,
    ) -> None:
        await self._update_snapshot(
            state="applying",
            current_stage="apply",
            stage_detail={
                "phase": "apply",
                "scope": "cell",
                "side": _side_to_module_name(side),
                "shelf_idx": int(target.shelf_idx),
                "cell_idx": int(target.cell_idx),
            },
        )

        config = get_root_config()
        scan_config = config.robot.grid.scan
        grid_path = Path(resolve_grid_path(config))
        grid = laser_module.load_grid(grid_path)
        laser_module.apply_single_cell_shelf_analysis(
            analysis=analysis,
            grid=grid,
            grid_path=grid_path,
            scan_config=scan_config,
            side=side,
            target=target,
        )
        worker._robot._grid = laser_module.load_grid(grid_path)


    async def _get_current_calibration_params(self, worker: RobotWorker) -> dict[str, float]:
        front = await worker._robot._tool.get_param("ROTATION_FRONT")
        back = await worker._robot._tool.get_param("ROTATION_BACK")
        front_stage_extension = await worker._robot._tool.get_param("STAGE_EXTENSION_SHELF_FRONT")
        back_stage_extension = await worker._robot._tool.get_param("STAGE_EXTENSION_SHELF_BACK")

        if front is None or back is None or front_stage_extension is None or back_stage_extension is None:
            raise ValueError("Current calibration parameters are not fully configured")

        return {
            "ROTATION_FRONT": float(front),
            "ROTATION_BACK": float(back),
            "STAGE_EXTENSION_SHELF_FRONT": float(front_stage_extension),
            "STAGE_EXTENSION_SHELF_BACK": float(back_stage_extension),
        }


    def _restore_in_memory_calibration_params(self, worker: RobotWorker, params: dict[str, float]) -> None:
        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict["ROTATION_FRONT"] = float(params["ROTATION_FRONT"])
        params_dict["ROTATION_BACK"] = float(params["ROTATION_BACK"])
        params_dict["STAGE_EXTENSION_SHELF_FRONT"] = float(params["STAGE_EXTENSION_SHELF_FRONT"])
        params_dict["STAGE_EXTENSION_SHELF_BACK"] = float(params["STAGE_EXTENSION_SHELF_BACK"])
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)

