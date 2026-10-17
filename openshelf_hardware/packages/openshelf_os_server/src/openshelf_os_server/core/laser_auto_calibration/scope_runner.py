# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import copy
from pathlib import Path
from statistics import median

from openshelf_os.laser_calibration.defaults import EXPECTED_HOLE_HEIGHT_MM, Z_OFFSET_MODEL_PARAM_TOLERANCE_MM

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
    expected_hole_height_mm = float(EXPECTED_HOLE_HEIGHT_MM)
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

def _scope_derives_from_the_datum(requested_scope: LaserAutoCalibrationScope) -> bool:
    """Whether this run's scan holds the bottom-left datum it would re-frame from.

    Delegates to the package so the validation and apply steps share one rule: a full-module or
    side scan derives (from all manual cells), a shelf-0 or cell-0:0 rescan derives from that
    opening, and a rescan of any other opening has no datum to work from.
    """
    return (
        laser_module.select_datum_derivation_scope(
            mode=requested_scope.mode,
            shelf_idx=requested_scope.shelf_idx,
            cell_idx=requested_scope.cell_idx,
        )
        is not None
    )


def _validate_laser_auto_calibration_config(
    *,
    scan_config: Any,
    requested_scope: LaserAutoCalibrationScope,
    front_bounds: ScanBounds | None = None,
    back_bounds: ScanBounds | None = None,
) -> None:
    laser_module.require_laser_offsets(scan_config)

    if front_bounds is None:
        front_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.FRONT)

    if back_bounds is None:
        back_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.BACK)

    # The configured offset is required even for a run that re-derives it: `derive_reference_offsets`
    # predicts the manual cells' detected positions from it, and refuses without one because at a
    # ~105mm opening pitch predicting from zero matches the neighbouring opening. Checking it here
    # turns a request that can never succeed into a refusal before any motion, instead of a failure
    # after the angle/stage calibration and a full side scan.
    if requested_scope.mode == "all" or requested_scope.mode == "both":
        laser_module.validate_scan_bounds(front_bounds, "front scan bounds")
        laser_module.validate_scan_bounds(back_bounds, "back scan bounds")
        laser_module.validate_laser_structure_parameters(
            scan_config=scan_config,
            side=StorageModuleSide.FRONT,
        )
        laser_module.validate_laser_structure_parameters(
            scan_config=scan_config,
            side=StorageModuleSide.BACK,
        )
        return

    if requested_scope.side == StorageModuleSide.FRONT:
        laser_module.validate_scan_bounds(front_bounds, "front scan bounds")
        if requested_scope.mode == "side":
            laser_module.validate_laser_structure_parameters(
                scan_config=scan_config,
                side=StorageModuleSide.FRONT,
            )
        elif _scope_derives_from_the_datum(requested_scope):
            # A shelf/cell rescan that re-frames from the datum predicts its matches from the
            # configured offset exactly like a full-side run, so a request with none configured
            # can never succeed and is refused here rather than after the scan.
            laser_module.require_scan_xy_correction(
                scan_config=scan_config,
                side=StorageModuleSide.FRONT,
            )
        return

    if requested_scope.side == StorageModuleSide.BACK:
        laser_module.validate_scan_bounds(back_bounds, "back scan bounds")
        if requested_scope.mode == "side":
            laser_module.validate_laser_structure_parameters(
                scan_config=scan_config,
                side=StorageModuleSide.BACK,
            )
        elif _scope_derives_from_the_datum(requested_scope):
            laser_module.require_scan_xy_correction(
                scan_config=scan_config,
                side=StorageModuleSide.BACK,
            )


class ScopeRunnerMixin:
    def _validate_requested_scope_config(
        self,
        requested_scope: LaserAutoCalibrationScope,
        worker: RobotWorker | None = None,
    ) -> None:
        scan_config = get_root_config().robot.grid.scan
        front_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.FRONT)
        back_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.BACK)
        _validate_laser_auto_calibration_config(
            scan_config=scan_config,
            requested_scope=requested_scope,
            front_bounds=front_bounds,
            back_bounds=back_bounds,
        )

        self._validate_reference_datum_cells(requested_scope, worker)
        self._validate_z_offset_sources(requested_scope, worker)

    def _validate_z_offset_sources(
        self,
        requested_scope: LaserAutoCalibrationScope,
        worker: RobotWorker | None,
    ) -> None:
        """Fail before any motion when a side's z offsets could not be resolved at the apply.

        A full-side apply needs a z source: the probe, a hand-calibrated cell whose z was saved
        with its stage extension recorded, or a configured model still pinned to the live
        Z_EXTENSION_PLACE. Stage extension drift is only refused for a stamp too far to carry,
        as the apply does; it carries every other source to the live stage. That carry check
        covers every scope that derives from the datum (with or without the probe, which reads
        the same datum), limited to the cells the scope derives from. Shelf and cell rescans keep
        the existing z when nothing resolves, so the z source itself is only required for side
        and module scopes without the probe.
        """
        derivation_scope = laser_module.select_datum_derivation_scope(
            mode=requested_scope.mode,
            shelf_idx=requested_scope.shelf_idx,
            cell_idx=requested_scope.cell_idx,
        )

        if derivation_scope is None:
            return

        config = get_root_config()
        scan_config = config.robot.grid.scan
        needs_z_source = (
            requested_scope.mode in ("all", "both", "side")
            and _z_stall_requested(scan_config) is not True
        )
        grid = _get_worker_grid(worker)
        z_place_value = getattr(config.robot.tool.parameters, "Z_EXTENSION_PLACE", None)
        sides: list[StorageModuleSide] = (
            [StorageModuleSide.FRONT, StorageModuleSide.BACK]
            if requested_scope.mode in ("all", "both")
            else ([requested_scope.side] if requested_scope.side is not None else [])
        )
        problems: list[str] = []
        missing_source = False

        for side in sides:
            side_name = "front" if side == StorageModuleSide.FRONT else "back"
            stage_extension_value = getattr(
                config.robot.tool.parameters, _side_to_stage_extension_param_name(side), None
            )

            try:
                has_stamped_datum = grid is not None and laser_module.check_stamped_reference_carry(
                    grid=grid,
                    side=side,
                    stage_extension_mm=None if stage_extension_value is None else float(stage_extension_value),
                    shelf_idx=derivation_scope[0],
                    cell_idx=derivation_scope[1],
                )
            except ValueError as exc:
                problems.append(str(exc).rstrip("."))
                continue

            if has_stamped_datum is True or needs_z_source is False:
                continue

            model = laser_module.get_wall_depth_to_gripper_z_model(scan_config=scan_config, side=side)

            if model is None:
                missing_source = True
                problems.append(
                    f"the {side_name} module has no z source: no hand-calibrated cell with its stage "
                    f"extension recorded and no robot.grid.scan.{side_name}_wall_depth_to_gripper_z_model"
                )
                continue

            if z_place_value is not None:
                try:
                    laser_module.require_z_offset_model_matches_z_place(
                        model=model,
                        side=side,
                        z_place_mm=float(z_place_value),
                        tolerance_mm=float(Z_OFFSET_MODEL_PARAM_TOLERANCE_MM),
                    )
                except ValueError as exc:
                    missing_source = True
                    problems.append(str(exc).rstrip("."))

        if len(problems) > 0:
            # The probe reads the same datum, so it is only a way out when a side lacks a source,
            # never for a stamp too far to carry.
            probe_hint = ", or enable the z stall probe" if missing_source is True else ""
            raise ValueError(
                f"auto calibration cannot resolve cell z offsets: {'; '.join(problems)}. In cell calibration "
                "extend the stage to the shelf extension, jog the bottom-left reference cell's z onto the "
                f"tray and save it (the stage extension is recorded only when the z changes){probe_hint}, "
                "then re-run"
            )

    def _validate_reference_datum_cells(
        self,
        requested_scope: LaserAutoCalibrationScope,
        worker: RobotWorker | None,
    ) -> None:
        """Fail before any motion unless every side in scope carries the hand-calibrated datum.

        The bottom-left cell of each side is the frame every derived offset is measured from, so
        a run that cannot use it has nothing to calibrate against and must not start.
        """
        grid = _get_worker_grid(worker)

        if grid is None:
            raise ValueError(
                "auto calibration needs the storage grid to check the hand-calibrated bottom-left "
                "reference cells, and the robot has none loaded"
            )

        sides: list[StorageModuleSide] = (
            [StorageModuleSide.FRONT, StorageModuleSide.BACK]
            if requested_scope.mode in ("all", "both")
            else ([requested_scope.side] if requested_scope.side is not None else [])
        )
        missing: list[str] = []

        for side in sides:
            if laser_module.select_reference_datum_cell(grid=grid, side=side) is not None:
                continue

            side_name = "front" if side == StorageModuleSide.FRONT else "back"
            missing.append(
                f"the {side_name} module's bottom-left cell ("
                f"{laser_module.REFERENCE_DATUM_SHELF_IDX}:{laser_module.REFERENCE_DATUM_CELL_IDX})"
            )

        if len(missing) > 0:
            raise ValueError(
                "auto calibration frames every offset from the hand-calibrated bottom-left cell "
                f"of each side, and these are missing or not marked manual: {', '.join(missing)}; "
                "precalibrate each one in cell calibration and tick 'keep manual' when saving it, "
                "then re-run"
            )

    async def _run_requested_scope(
        self,
        worker: RobotWorker,
        requested_scope: LaserAutoCalibrationScope,
    ) -> None:
        scan_config = get_root_config().robot.grid.scan
        front_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.FRONT)
        back_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.BACK)

        if requested_scope.mode in ("all", "both"):
            await self._run_all_sides(
                worker,
                requested_scope.include_angle_calibration,
                requested_scope.include_extension_calibration,
                front_bounds=front_bounds,
                back_bounds=back_bounds,
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
        front_bounds: ScanBounds | None = None,
        back_bounds: ScanBounds | None = None,
    ) -> None:
        completed_results: list[_CompletedSideResult] = []
        scan_config = get_root_config().robot.grid.scan
        if front_bounds is None:
            front_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.FRONT)
        if back_bounds is None:
            back_bounds = _build_bounds_for_side(scan_config, StorageModuleSide.BACK)
        back_angle_result: LaserAutoCalibrationAngleResult | None = None
        front_angle_result: LaserAutoCalibrationAngleResult | None = None

        if include_angle_calibration is True:
            back_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.BACK, back_bounds)
            await self._apply_angle_calibration(worker, StorageModuleSide.BACK, back_angle_result)

            front_angle_result = await self._run_angle_calibration_for_side(worker, StorageModuleSide.FRONT, front_bounds)
            await self._apply_angle_calibration(worker, StorageModuleSide.FRONT, front_angle_result)

        if include_extension_calibration is True:
            back_probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
                worker,
                StorageModuleSide.BACK,
                back_bounds,
                top_shelf_y_mm=None if back_angle_result is None else float(back_angle_result.capture_y_mm),
            )
            back_stage_extension_mm = await self._run_stage_extension_calibration_for_side(
                worker,
                StorageModuleSide.BACK,
                back_bounds,
                probe_y_mm=back_probe_y_mm,
            )
            await self._apply_stage_extension_calibration(
                worker,
                StorageModuleSide.BACK,
                back_stage_extension_mm,
            )

            front_probe_y_mm = await self._resolve_stage_extension_probe_y_mm(
                worker,
                StorageModuleSide.FRONT,
                front_bounds,
                top_shelf_y_mm=None if front_angle_result is None else float(front_angle_result.capture_y_mm),
            )
            front_stage_extension_mm = await self._run_stage_extension_calibration_for_side(
                worker,
                StorageModuleSide.FRONT,
                front_bounds,
                probe_y_mm=front_probe_y_mm,
            )
            await self._apply_stage_extension_calibration(
                worker,
                StorageModuleSide.FRONT,
                front_stage_extension_mm,
            )

        completed_results.append(await self._run_side(worker, StorageModuleSide.BACK, back_angle_result))
        completed_results.append(await self._run_side(worker, StorageModuleSide.FRONT, front_angle_result))

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
            float(EXPECTED_HOLE_HEIGHT_MM)
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
        # An abort can land mid grip-edge probe with the fingers inside a tray hole, possibly before
        # the probe's own close: release, then withdraw, before X moves.
        await worker._robot._tool._gs.close()

        await worker._robot._tool.retract_empty()

        if worker._robot._tool._mx is not None:
            await worker._robot._tool._mx.move_to_position(angle_module.BACK_MODULE_ROTATION_SAFE_X_MM)

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

