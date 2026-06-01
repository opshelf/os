# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import json
from pathlib import Path
from statistics import median

from .models import *



def _get_xy_reference_target(scan_config: Any, side: StorageModuleSide) -> laser_module.SingleCellRescanTarget:
    xy_reference = laser_module.get_scan_xy_reference(scan_config=scan_config, side=side)
    return laser_module.SingleCellRescanTarget(
        shelf_idx=int(xy_reference.shelf_idx),
        cell_idx=int(xy_reference.cell_idx),
    )


def _get_z_reference_target(scan_config: Any, side: StorageModuleSide) -> laser_module.SingleCellRescanTarget:
    z_reference = laser_module.get_scan_z_reference(scan_config=scan_config, side=side)
    return laser_module.SingleCellRescanTarget(
        shelf_idx=int(z_reference[0]),
        cell_idx=int(z_reference[1]),
    )


def _rescan_targets_match(
    left: laser_module.SingleCellRescanTarget,
    right: laser_module.SingleCellRescanTarget,
) -> bool:
    return int(left.shelf_idx) == int(right.shelf_idx) and int(left.cell_idx) == int(right.cell_idx)


def _rescan_target_key(target: laser_module.SingleCellRescanTarget) -> tuple[int, int]:
    return (int(target.shelf_idx), int(target.cell_idx))


def _resolve_reference_wall_depth_mm(
    analysis: Any,
    candidate: laser_module.GapCandidateRegion,
) -> float:
    if candidate.wall_depth_mm is not None and float(candidate.wall_depth_mm) > 0.0:
        return float(candidate.wall_depth_mm)

    if getattr(analysis, "wall_depth_mm", None) is not None and float(analysis.wall_depth_mm) > 0.0:
        return float(analysis.wall_depth_mm)

    raise ValueError("Reference cell scan did not resolve a valid wall depth")


def _build_reference_apply_metadata(
    xy_reference_detected_cell: Any | None,
    z_reference_wall_depth_mm: float | None,
    xy_reference_target: laser_module.SingleCellRescanTarget,
    z_reference_target: laser_module.SingleCellRescanTarget,
) -> dict[str, Any]:
    if xy_reference_detected_cell is None:
        raise ValueError("Reference cell scans did not resolve an XY reference cell")

    if z_reference_wall_depth_mm is None:
        raise ValueError("Reference cell scans did not resolve a Z reference wall depth")

    return {
        "xy_reference_detected_cell": xy_reference_detected_cell.model_dump(),
        "z_reference_wall_depth_mm": float(z_reference_wall_depth_mm),
        "xy_reference_target": {
            "shelf_idx": int(xy_reference_target.shelf_idx),
            "cell_idx": int(xy_reference_target.cell_idx),
        },
        "z_reference_target": {
            "shelf_idx": int(z_reference_target.shelf_idx),
            "cell_idx": int(z_reference_target.cell_idx),
        },
    }


def _persist_reference_apply_metadata(analysis: Any, reference_apply: dict[str, Any]) -> None:
    run_parameters_path = Path(analysis.metadata_path).resolve().parent / "run_parameters.json"
    run_parameters_document = laser_module.load_json_file(run_parameters_path)
    run_parameters = dict(run_parameters_document.get("run_parameters", {}))
    run_parameters["reference_apply"] = reference_apply
    run_parameters_document["run_parameters"] = run_parameters
    run_parameters_path.write_text(json.dumps(run_parameters_document, indent="\t"), encoding="utf-8")


class ScopeRunnerMixin:
    async def _run_requested_scope(
        self,
        worker: RobotWorker,
        requested_scope: LaserAutoCalibrationScope,
    ) -> None:
        if requested_scope.mode == "all":
            await self._run_all_sides(worker, requested_scope.include_angle_calibration)

            if requested_scope.include_io_box is True:
                await self._run_io_box_calibration(worker)

            return

        if requested_scope.mode == "io_box":
            await self._run_io_box_calibration(worker)
            return

        if requested_scope.side is None:
            raise ValueError("A scoped auto calibration request requires a side")

        side = requested_scope.side

        if requested_scope.mode == "side":
            await self._run_single_side(worker, side, requested_scope.include_angle_calibration)

            if requested_scope.include_io_box is True:
                await self._run_io_box_calibration(worker)

            return

        if requested_scope.mode == "shelf":
            if requested_scope.shelf_idx is None:
                raise ValueError("Single-shelf auto calibration requires a shelf index")

            await self._run_single_shelf(worker, side, int(requested_scope.shelf_idx))

            if requested_scope.include_io_box is True:
                await self._run_io_box_calibration(worker)

            return

        if requested_scope.mode == "cell":
            if requested_scope.shelf_idx is None or requested_scope.cell_idx is None:
                raise ValueError("Single-cell auto calibration requires shelf and cell indices")

            await self._run_single_cell(
                worker,
                side,
                laser_module.SingleCellRescanTarget(
                    shelf_idx=int(requested_scope.shelf_idx),
                    cell_idx=int(requested_scope.cell_idx),
                ),
            )

            if requested_scope.include_io_box is True:
                await self._run_io_box_calibration(worker)

            return

        raise ValueError(f"Unsupported auto calibration scope: {requested_scope.mode}")

    async def _run_all_sides(self, worker: RobotWorker, include_angle_calibration: bool = True) -> None:
        completed_results: list[_CompletedSideResult] = []
        scan_config = get_root_config().robot.grid.scan
        left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
        right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)
        right_angle_result: LaserAutoCalibrationAngleResult | None = None
        left_angle_result: LaserAutoCalibrationAngleResult | None = None

        if include_angle_calibration is True:
            right_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.RIGHT, right_bounds)
            await self._apply_angle_calibration_in_memory(worker, StorageModuleSide.RIGHT, right_angle_result)

            left_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.LEFT, left_bounds)
            await self._apply_angle_calibration_in_memory(worker, StorageModuleSide.LEFT, left_angle_result)

        right_probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
            worker,
            StorageModuleSide.RIGHT,
            right_bounds,
            top_shelf_y_mm=None if right_angle_result is None else float(right_angle_result.capture_y_mm),
        )
        right_stage_extension_mm = await self._run_stage_extension_calibration_for_side(
            worker,
            StorageModuleSide.RIGHT,
            right_bounds,
            probe_y_mm=right_probe_y_mm,
        )
        await self._apply_stage_extension_calibration_in_memory(
            worker,
            StorageModuleSide.RIGHT,
            right_stage_extension_mm,
        )

        left_probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
            worker,
            StorageModuleSide.LEFT,
            left_bounds,
            top_shelf_y_mm=None if left_angle_result is None else float(left_angle_result.capture_y_mm),
        )
        left_stage_extension_mm = await self._run_stage_extension_calibration_for_side(
            worker,
            StorageModuleSide.LEFT,
            left_bounds,
            probe_y_mm=left_probe_y_mm,
        )
        await self._apply_stage_extension_calibration_in_memory(
            worker,
            StorageModuleSide.LEFT,
            left_stage_extension_mm,
        )

        completed_results.append(await self._run_side(worker, StorageModuleSide.RIGHT, right_angle_result))
        completed_results.append(await self._run_side(worker, StorageModuleSide.LEFT, left_angle_result))

        await self._persist_completed_results(worker, completed_results)

    async def _run_single_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        include_angle_calibration: bool = True,
    ) -> None:
        completed_results: list[_CompletedSideResult] = []
        scan_config = get_root_config().robot.grid.scan
        bounds = _build_bounds_for_side(scan_config, side)
        angle_result: LaserAutoCalibrationAngleResult | None = None

        if include_angle_calibration is True:
            angle_result = await self._run_angle_calibration_for_side(worker, side, bounds)
            await self._apply_angle_calibration_in_memory(worker, side, angle_result)

        probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
            worker,
            side,
            bounds,
            top_shelf_y_mm=None if angle_result is None else float(angle_result.capture_y_mm),
        )
        stage_extension_mm = await self._run_stage_extension_calibration_for_side(
            worker,
            side,
            bounds,
            probe_y_mm=probe_y_mm,
        )
        await self._apply_stage_extension_calibration_in_memory(worker, side, stage_extension_mm)
        completed_results.append(await self._run_side(worker, side, angle_result))
        await self._persist_completed_results(worker, completed_results)

    async def _run_reference_cell_scan(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        target: laser_module.SingleCellRescanTarget,
    ) -> tuple[Any, laser_module.GapCandidateRegion, Any]:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        grid_path = Path(resolve_grid_path(config))
        grid = laser_module.load_grid(grid_path)
        candidate = laser_module.build_single_cell_rescan_candidate(
            grid=grid,
            side=side,
            target=target,
            scan_config=scan_config,
        )
        bounds = laser_module.build_single_cell_rescan_bounds(
            candidate=candidate,
            scan_config=scan_config,
        )
        analysis = await self._run_scan_analysis(
            worker,
            side,
            bounds,
            seeded_candidates=[candidate],
            rescan_target=target,
        )
        detected_cell = laser_module.find_detected_cell_for_candidate(
            analysis=analysis,
            candidate=candidate,
        )

        if detected_cell is None:
            raise ValueError(
                f"No detected cell matched reference calibration target {target.shelf_idx}:{target.cell_idx}"
            )

        return analysis, candidate, detected_cell


    async def _run_partial_reference_scans(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        *,
        reuse_target: laser_module.SingleCellRescanTarget | None = None,
        reuse_analysis: Any | None = None,
        reuse_candidate: laser_module.GapCandidateRegion | None = None,
        reuse_detected_cell: Any | None = None,
    ) -> tuple[Any | None, float | None]:
        scan_config = get_root_config().robot.grid.scan
        xy_reference_target = _get_xy_reference_target(scan_config, side)
        z_reference_target = _get_z_reference_target(scan_config, side)
        xy_reference_detected_cell: Any | None = None
        z_reference_wall_depth_mm: float | None = None
        reference_results: dict[tuple[int, int], tuple[Any, laser_module.GapCandidateRegion, Any]] = {}

        if (
            reuse_target is not None
            and reuse_analysis is not None
            and reuse_candidate is not None
            and reuse_detected_cell is not None
        ):
            reference_results[_rescan_target_key(reuse_target)] = (
                reuse_analysis,
                reuse_candidate,
                reuse_detected_cell,
            )

        for reference_target in (xy_reference_target, z_reference_target):
            reference_key = _rescan_target_key(reference_target)

            if reference_key not in reference_results:
                reference_results[reference_key] = await self._run_reference_cell_scan(
                    worker,
                    side,
                    reference_target,
                )

            reference_analysis, reference_candidate, reference_detected_cell = reference_results[reference_key]

            if _rescan_targets_match(reference_target, xy_reference_target) is True:
                xy_reference_detected_cell = reference_detected_cell

            if _rescan_targets_match(reference_target, z_reference_target) is True:
                z_reference_wall_depth_mm = _resolve_reference_wall_depth_mm(
                    reference_analysis,
                    reference_candidate,
                )

        return xy_reference_detected_cell, z_reference_wall_depth_mm

    async def _run_single_shelf(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        shelf_idx: int,
    ) -> None:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        grid_path = Path(resolve_grid_path(config))
        grid = laser_module.load_grid(grid_path)
        bounds = _build_bounds_for_side(scan_config, side)
        target_shelf = laser_module.resolve_target_shelf(
            grid=grid,
            side=side,
            shelf_idx=shelf_idx,
        )
        target_cell_y_values = [
            float(cell.calibration_point.y)
            for cell in target_shelf.cells
            if cell.calibration_point is not None
        ]

        if len(target_cell_y_values) == 0:
            raise ValueError(f"Shelf {shelf_idx} has no calibration points on {side.value}")

        target_center_y_mm = float(median(target_cell_y_values))
        local_y_margin_mm = max(
            80.0,
            float(scan_config.expected_hole_height_mm)
            + max(float(offset_mm) for offset_mm in laser_module.get_shelf_hole_probe_y_offsets_mm(scan_config))
            + 30.0,
        )
        local_bounds = ScanBounds(
            x_min=bounds.x_min,
            x_max=bounds.x_max,
            y_min=max(bounds.y_min, target_center_y_mm - local_y_margin_mm),
            y_max=min(bounds.y_max, target_center_y_mm + local_y_margin_mm),
        )
        xy_reference_detected_cell, z_reference_wall_depth_mm = await self._run_partial_reference_scans(
            worker,
            side,
        )
        reference_apply = _build_reference_apply_metadata(
            xy_reference_detected_cell,
            z_reference_wall_depth_mm,
            _get_xy_reference_target(scan_config, side),
            _get_z_reference_target(scan_config, side),
        )
        analysis = await self._run_scan_analysis(
            worker,
            side,
            local_bounds,
            known_shelf_edge_y=None,
            should_validate_reference_bounds=False,
            run_scope={
                "mode": "shelf",
                "side": side.value,
                "shelf_idx": int(shelf_idx),
            },
            reference_apply=reference_apply,
        )
        await self._persist_single_shelf_result(
            worker,
            side,
            analysis,
            shelf_idx,
            xy_reference_detected_cell=xy_reference_detected_cell,
            z_reference_wall_depth_mm=z_reference_wall_depth_mm,
        )

    async def _run_single_cell(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        target: laser_module.SingleCellRescanTarget,
    ) -> None:
        analysis, candidate, detected_cell = await self._run_reference_cell_scan(
            worker,
            side,
            target,
        )
        xy_reference_detected_cell, z_reference_wall_depth_mm = await self._run_partial_reference_scans(
            worker,
            side,
            reuse_target=target,
            reuse_analysis=analysis,
            reuse_candidate=candidate,
            reuse_detected_cell=detected_cell,
        )
        scan_config = get_root_config().robot.grid.scan
        reference_apply = _build_reference_apply_metadata(
            xy_reference_detected_cell,
            z_reference_wall_depth_mm,
            _get_xy_reference_target(scan_config, side),
            _get_z_reference_target(scan_config, side),
        )
        _persist_reference_apply_metadata(analysis, reference_apply)
        await self._persist_single_cell_result(
            worker,
            side,
            analysis,
            target,
            candidate,
            xy_reference_detected_cell=xy_reference_detected_cell,
            z_reference_wall_depth_mm=z_reference_wall_depth_mm,
        )

    async def _home_after_abort(self, worker: RobotWorker) -> None:
        if worker._robot._tool._mx is not None:
            await worker._robot._tool._mx.move_to_position(angle_module.RIGHT_MODULE_ROTATION_SAFE_X_MM)

        await worker._robot._tool.home()

        try:
            await worker._robot._io.home_door()
        except Exception:
            logger.warning("laser_auto_calibration_abort_door_home_failed", exc_info=True)

        worker.clear_needs_home()
        await worker.get_status_snapshot(force_refresh=True)

    async def _run_side(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        angle_result: LaserAutoCalibrationAngleResult | None,
    ) -> _CompletedSideResult:
        config = get_root_config()
        scan_config = config.robot.grid.scan
        bounds = _build_bounds_for_side(scan_config, side)

        if angle_result is not None:
            await self._apply_angle_calibration_in_memory(worker, side, angle_result)
        analysis = await self._run_scan_analysis(worker, side, bounds)

        return _CompletedSideResult(
            side=side,
            bounds=bounds,
            analysis=analysis,
            angle_result=angle_result,
        )

