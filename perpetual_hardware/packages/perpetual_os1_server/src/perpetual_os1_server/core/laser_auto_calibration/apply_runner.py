# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

from .models import *


class ApplyRunnerMixin:
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

            if side_state is None or side_state.stage_extension_mm is None:
                raise ValueError(f"Missing stage extension calibration result for {_side_to_module_name(completed_result.side)}")

            params_dict[rotation_param] = completed_result.angle_result.updated_rotation_deg
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

            existing_shelves = left_module

            if completed_result.side == StorageModuleSide.RIGHT:
                existing_shelves = right_module

            scanned_shelves = laser_module.build_shelves_from_scan(
                side=completed_result.side,
                analysis=completed_result.analysis,
                existing_shelves=existing_shelves,
            )
            laser_module.apply_xy_reference_offsets(
                scanned_shelves=scanned_shelves,
                scan_config=scan_config,
                side=completed_result.side,
            )
            laser_module.apply_wall_depth_z_offsets(
                analysis=completed_result.analysis,
                scanned_shelves=scanned_shelves,
                metadata_path=str(completed_result.analysis.metadata_path),
                scan_config=scan_config,
                side=completed_result.side,
                current_stage_extension_mm=float(params_dict[_side_to_stage_extension_param_name(completed_result.side)]),
            )

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
        laser_module.apply_single_shelf_analysis(
            analysis=analysis,
            grid=grid,
            grid_path=grid_path,
            scan_config=scan_config,
            side=side,
            target_shelf_idx=shelf_idx,
            current_stage_extension_mm=float(
                getattr(config.robot.tool.parameters, _side_to_stage_extension_param_name(side))
            ),
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
            wall_depth_mm=float(analysis.wall_depth_mm),
            scan_config=scan_config,
            current_stage_extension_mm=float(
                getattr(config.robot.tool.parameters, _side_to_stage_extension_param_name(side))
            ),
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

