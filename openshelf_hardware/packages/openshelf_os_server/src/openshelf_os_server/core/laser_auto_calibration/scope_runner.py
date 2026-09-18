# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import copy
from pathlib import Path
from statistics import median

from .models import *



def _get_shelf_calibration_center_y(shelf: Any) -> float | None:
    y_values = [
        float(cell.calibration_point.y)
        for cell in shelf.cells
        if cell.calibration_point is not None
    ]

    if len(y_values) == 0:
        return None

    return float(median(y_values))


def _get_side_shelves(grid: Any, side: StorageModuleSide) -> list[Any]:
    if hasattr(grid, "get_side") is True:
        return list(grid.get_side(side))

    return list(getattr(grid, side.value, []))


def _get_worker_grid(worker: Any) -> Any:
    robot = getattr(worker, "_robot", None)
    grid = getattr(robot, "_grid", None)

    if grid is not None:
        return grid

    try:
        config = get_root_config()
        return laser_module.load_grid(Path(resolve_grid_path(config)))
    except Exception:
        return None


def _build_single_shelf_scan_bounds(
    *,
    grid: Any,
    side: StorageModuleSide,
    target_center_y_mm: float,
    base_bounds: ScanBounds,
    local_y_margin_mm: float,
    shelf_idx: int,
    scan_config: Any,
) -> ScanBounds:
    y_min = max(base_bounds.y_min, target_center_y_mm - local_y_margin_mm)
    y_max = min(base_bounds.y_max, target_center_y_mm + local_y_margin_mm)
    hole_probe_y_offsets_mm = laser_module.get_shelf_hole_probe_y_offsets_mm(scan_config)
    reference_offset_mm = float(hole_probe_y_offsets_mm[0])
    laser_offset_y_mm = float(getattr(scan_config, "laser_offset_y_mm", 0.0))
    target_edge_y_mm = target_center_y_mm + laser_offset_y_mm - reference_offset_mm
    shelf_edges: list[tuple[int, float]] = []

    for shelf in _get_side_shelves(grid, side):
        center_y_mm = _get_shelf_calibration_center_y(shelf)

        if center_y_mm is None:
            continue

        edge_y_mm = center_y_mm + laser_offset_y_mm - reference_offset_mm
        shelf_edges.append((int(shelf.shelf_idx), edge_y_mm))

    previous_edges = [
        edge_y_mm
        for current_shelf_idx, edge_y_mm in shelf_edges
        if current_shelf_idx != shelf_idx and edge_y_mm < target_edge_y_mm
    ]
    next_edges = [
        edge_y_mm
        for current_shelf_idx, edge_y_mm in shelf_edges
        if current_shelf_idx != shelf_idx and edge_y_mm > target_edge_y_mm
    ]

    if len(previous_edges) > 0:
        previous_edge_y_mm = max(previous_edges)
        y_min = max(y_min, (previous_edge_y_mm + target_edge_y_mm) / 2.0 + reference_offset_mm)

    if len(next_edges) > 0:
        next_edge_y_mm = min(next_edges)
        y_max = min(y_max, (target_edge_y_mm + next_edge_y_mm) / 2.0 + reference_offset_mm)

    return ScanBounds(
        x_min=base_bounds.x_min,
        x_max=base_bounds.x_max,
        y_min=y_min,
        y_max=y_max,
    )


def _build_single_shelf_scan_config(scan_config: Any) -> Any:
    probe_offsets = laser_module.get_shelf_hole_probe_y_offsets_mm(scan_config)
    primary_probe_offset_mm = float(probe_offsets[0])
    expected_hole_height_mm = float(scan_config.expected_hole_height_mm)
    opening_center_offset_mm = max(
        0.0,
        primary_probe_offset_mm - (expected_hole_height_mm * 2.0 / 3.0),
    )

    if hasattr(scan_config, "model_copy") is True:
        return scan_config.model_copy(
            update={"shelf_hole_probe_y_offsets_mm": [opening_center_offset_mm]}
        )

    copied_scan_config = copy.copy(scan_config)
    copied_scan_config.shelf_hole_probe_y_offsets_mm = [opening_center_offset_mm]
    return copied_scan_config

def _validate_laser_auto_calibration_config(
    *,
    scan_config: Any,
    requested_scope: LaserAutoCalibrationScope,
    left_bounds: ScanBounds | None = None,
    right_bounds: ScanBounds | None = None,
) -> None:
    laser_module.require_laser_offsets(scan_config)

    if left_bounds is None:
        left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)

    if right_bounds is None:
        right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)

    # A run that derives its own reference frame supplies the offset the configured-value
    # check exists to insist on, so demanding one first would block exactly the machines that
    # do not have a trustworthy value yet. The laser offsets and bounds still apply.
    should_require_configured_offset = requested_scope.derive_offsets_from_manual_cells is not True

    if requested_scope.mode == "all" or requested_scope.mode == "both":
        laser_module.validate_scan_bounds(left_bounds, "left scan bounds")
        laser_module.validate_scan_bounds(right_bounds, "right scan bounds")

        if should_require_configured_offset is True:
            laser_module.validate_laser_structure_parameters(
                scan_config=scan_config,
                side=StorageModuleSide.LEFT,
            )
            laser_module.validate_laser_structure_parameters(
                scan_config=scan_config,
                side=StorageModuleSide.RIGHT,
            )
        return

    if requested_scope.side == StorageModuleSide.LEFT:
        laser_module.validate_scan_bounds(left_bounds, "left scan bounds")
        if requested_scope.mode == "side" and should_require_configured_offset is True:
            laser_module.validate_laser_structure_parameters(
                scan_config=scan_config,
                side=StorageModuleSide.LEFT,
            )
        return

    if requested_scope.side == StorageModuleSide.RIGHT:
        laser_module.validate_scan_bounds(right_bounds, "right scan bounds")
        if requested_scope.mode == "side" and should_require_configured_offset is True:
            laser_module.validate_laser_structure_parameters(
                scan_config=scan_config,
                side=StorageModuleSide.RIGHT,
            )


class ScopeRunnerMixin:
    def _validate_requested_scope_config(
        self,
        requested_scope: LaserAutoCalibrationScope,
        worker: RobotWorker | None = None,
    ) -> None:
        scan_config = get_root_config().robot.grid.scan
        left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
        right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)
        _validate_laser_auto_calibration_config(
            scan_config=scan_config,
            requested_scope=requested_scope,
            left_bounds=left_bounds,
            right_bounds=right_bounds,
        )

        if (
            requested_scope.derive_offsets_from_manual_cells is True
            or requested_scope.derive_z_model_from_stall is True
            or getattr(scan_config, "z_stall_anchor_enabled", False) is True
        ):
            self._validate_manual_reference_cells(requested_scope, worker)

    def _validate_manual_reference_cells(
        self,
        requested_scope: LaserAutoCalibrationScope,
        worker: RobotWorker | None,
    ) -> None:
        """Fail before any motion when a requested reference frame has no reference cells."""
        grid = _get_worker_grid(worker)

        if grid is None:
            raise ValueError(
                "a reference-frame or z-probe run needs the storage grid to find the manually "
                "calibrated reference cells, and the robot has none loaded"
            )

        sides: list[StorageModuleSide] = (
            [StorageModuleSide.LEFT, StorageModuleSide.RIGHT]
            if requested_scope.mode in ("all", "both")
            else ([requested_scope.side] if requested_scope.side is not None else [])
        )
        missing: list[str] = []

        for side in sides:
            cells = laser_module.select_manual_reference_cells(
                grid=grid,
                side=side,
                shelf_idx=requested_scope.shelf_idx,
                cell_idx=requested_scope.cell_idx if requested_scope.mode == "cell" else None,
            )

            if len(cells) == 0:
                missing.append(side.value)

        if len(missing) > 0:
            raise ValueError(
                "a reference-frame or z-probe run needs at least one cell marked manual in scope "
                f"on {', '.join(missing)}; tick 'keep manual' when saving the reference cell"
            )

    async def _run_requested_scope(
        self,
        worker: RobotWorker,
        requested_scope: LaserAutoCalibrationScope,
    ) -> None:
        scan_config = get_root_config().robot.grid.scan
        left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
        right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)

        if requested_scope.mode == "all":
            await self._run_all_sides(
                worker,
                requested_scope.include_angle_calibration,
                requested_scope.include_extension_calibration,
                left_bounds=left_bounds,
                right_bounds=right_bounds,
            )

            return

        if requested_scope.mode == "both":
            await self._run_all_sides(
                worker,
                requested_scope.include_angle_calibration,
                requested_scope.include_extension_calibration,
                left_bounds=left_bounds,
                right_bounds=right_bounds,
            )

            return

        if requested_scope.side is None:
            raise ValueError("A scoped auto calibration request requires a side")

        side = requested_scope.side

        if requested_scope.mode == "side":
            await self._run_single_side(
                worker,
                side,
                requested_scope.include_angle_calibration,
                requested_scope.include_extension_calibration,
            )

            return

        if requested_scope.mode == "shelf":
            if requested_scope.shelf_idx is None:
                raise ValueError("Single-shelf auto calibration requires a shelf index")

            await self._run_single_shelf(worker, side, int(requested_scope.shelf_idx))

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

            return

        raise ValueError(f"Unsupported auto calibration scope: {requested_scope.mode}")

    async def _run_all_sides(
        self,
        worker: RobotWorker,
        include_angle_calibration: bool = True,
        include_extension_calibration: bool = True,
        *,
        left_bounds: ScanBounds | None = None,
        right_bounds: ScanBounds | None = None,
    ) -> None:
        completed_results: list[_CompletedSideResult] = []
        scan_config = get_root_config().robot.grid.scan
        if left_bounds is None:
            left_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.LEFT)
        if right_bounds is None:
            right_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.RIGHT)
        right_angle_result: LaserAutoCalibrationAngleResult | None = None
        left_angle_result: LaserAutoCalibrationAngleResult | None = None

        if include_angle_calibration is True:
            right_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.RIGHT, right_bounds)
            await self._apply_angle_calibration(worker, StorageModuleSide.RIGHT, right_angle_result)

            left_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.LEFT, left_bounds)
            await self._apply_angle_calibration(worker, StorageModuleSide.LEFT, left_angle_result)

        if include_extension_calibration is True:
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
            await self._apply_stage_extension_calibration(
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
            await self._apply_stage_extension_calibration(
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
        include_extension_calibration: bool = True,
    ) -> None:
        completed_results: list[_CompletedSideResult] = []
        scan_config = get_root_config().robot.grid.scan
        bounds = _build_bounds_for_side(scan_config, side)
        angle_result: LaserAutoCalibrationAngleResult | None = None

        if include_angle_calibration is True:
            angle_result = await self._run_angle_calibration_for_side(worker, side, bounds)
            await self._apply_angle_calibration(worker, side, angle_result)

        if include_extension_calibration is True:
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
            await self._apply_stage_extension_calibration(worker, side, stage_extension_mm)

        completed_results.append(await self._run_side(worker, side, angle_result))
        await self._persist_completed_results(worker, completed_results)

    async def _run_single_cell_scan(
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
                f"No detected cell matched calibration target {target.shelf_idx}:{target.cell_idx}"
            )

        return analysis, candidate, detected_cell

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
        target_center_y_mm = _get_shelf_calibration_center_y(target_shelf)

        if target_center_y_mm is None:
            raise ValueError(f"Shelf {shelf_idx} has no calibration points on {side.value}")

        local_y_margin_mm = max(
            80.0,
            float(scan_config.expected_hole_height_mm)
            + max(float(offset_mm) for offset_mm in laser_module.get_shelf_hole_probe_y_offsets_mm(scan_config))
            + 30.0,
        )
        local_bounds = _build_single_shelf_scan_bounds(
            grid=grid,
            side=side,
            target_center_y_mm=target_center_y_mm,
            base_bounds=bounds,
            local_y_margin_mm=local_y_margin_mm,
            shelf_idx=shelf_idx,
            scan_config=scan_config,
        )
        single_shelf_scan_config = _build_single_shelf_scan_config(scan_config)
        known_shelf_edge_y = laser_module.build_single_shelf_known_edge_y(
            grid=grid,
            side=side,
            shelf_idx=shelf_idx,
            scan_config=single_shelf_scan_config,
        )
        analysis = await self._run_scan_analysis(
            worker,
            side,
            local_bounds,
            known_shelf_edge_y=known_shelf_edge_y,
            run_scope={
                "mode": "shelf",
                "side": side.value,
                "shelf_idx": int(shelf_idx),
            },
            scan_config_override=single_shelf_scan_config,
        )
        await self._persist_single_shelf_result(
            worker,
            side,
            analysis,
            shelf_idx,
        )

    async def _run_single_cell(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        target: laser_module.SingleCellRescanTarget,
    ) -> None:
        analysis, candidate, _ = await self._run_single_cell_scan(
            worker,
            side,
            target,
        )
        await self._persist_single_cell_result(
            worker,
            side,
            analysis,
            target,
            candidate,
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
            await self._apply_angle_calibration(worker, side, angle_result)
        analysis = await self._run_scan_analysis(worker, side, bounds)

        return _CompletedSideResult(
            side=side,
            bounds=bounds,
            analysis=analysis,
            angle_result=angle_result,
        )

