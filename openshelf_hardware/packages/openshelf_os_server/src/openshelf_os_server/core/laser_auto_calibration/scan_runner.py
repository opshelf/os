# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import argparse

from .models import *
from openshelf_os.common import CONFIG_PATH
from openshelf_os_server.core.logging_config import get_correlation_id


def _clamp_bounds_to_axis_travel(bounds: ScanBounds, tool: Any) -> ScanBounds:
    """Bounds past an axis limit name positions the gantry cannot reach.

    The motor clamps such a target to its limit, so every grid position beyond it is probed at
    the limit and recorded as if it were further out, and a flying pass leaves those bins empty
    for a slow standstill resample of the same spot.
    """
    limits: dict[str, tuple[float, float]] = {}

    for axis, motor in (("x", getattr(tool, "_mx", None)), ("y", getattr(tool, "_my", None))):
        params = getattr(motor, "_params", None)
        upper_mm = getattr(params, "lim", None)

        if not isinstance(upper_mm, (int, float)):
            continue

        lower_mm = getattr(params, "lower_lim", 0.0)
        limits[axis] = (float(lower_mm) if isinstance(lower_mm, (int, float)) else 0.0, float(upper_mm))

    x_lo, x_hi = limits.get("x", (bounds.x_min, bounds.x_max))
    y_lo, y_hi = limits.get("y", (bounds.y_min, bounds.y_max))
    clamped = ScanBounds(
        x_min=max(bounds.x_min, x_lo),
        x_max=min(bounds.x_max, x_hi),
        y_min=max(bounds.y_min, y_lo),
        y_max=min(bounds.y_max, y_hi),
    )

    if clamped.x_max <= clamped.x_min or clamped.y_max <= clamped.y_min:
        return bounds

    if clamped != bounds:
        logger.info(
            "laser_auto_calibration_bounds_clamped_to_axis_travel",
            extra={"bounds": bounds.model_dump(), "clamped": clamped.model_dump()},
        )

    return clamped


class ScanRunnerMixin:
    async def _run_scan_analysis(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        bounds: ScanBounds,
        *,
        known_shelf_edge_y: list[float] | None = None,
        seeded_candidates: list[laser_module.GapCandidateRegion] | None = None,
        seeded_wall_depth_mm: float | None = None,
        rescan_target: laser_module.SingleCellRescanTarget | None = None,
        run_scope: dict[str, Any] | None = None,
        scan_config_override: Any | None = None,
    ) -> Any:
        side_name = _side_to_module_name(side)
        bounds = _clamp_bounds_to_axis_travel(bounds, worker._robot._tool)
        config = get_root_config()
        scan_config = scan_config_override or config.robot.grid.scan
        base_settle_ms = int(laser_module.get_laser_sample_settle_ms())
        base_samples_per_point = int(laser_module.get_laser_samples_per_point())
        # Fine and coarse scans both honor the shared settling and sample count. Reducing
        # either pass to one read defeats the median filter and lets a transient laser reading
        # erase a narrow wall or move a detected edge by one scan step.
        settle_ms = int(laser_module.get_fine_settle_ms(base_settle_ms))
        samples_per_point = int(laser_module.get_fine_samples_per_point(base_samples_per_point))
        coarse_settle_ms = int(laser_module.get_coarse_settle_ms(base_settle_ms))
        coarse_samples_per_point = int(laser_module.get_coarse_samples_per_point(base_samples_per_point))
        raw_rescan_target: str | None = None
        raw_x_bounds: tuple[float, float] | None = (bounds.x_min, bounds.x_max)
        raw_y_bounds: tuple[float, float] | None = (bounds.y_min, bounds.y_max)

        if rescan_target is not None:
            raw_rescan_target = f"{rescan_target.shelf_idx}:{rescan_target.cell_idx}"
            raw_x_bounds = None
            raw_y_bounds = None

        args = argparse.Namespace(
            module=_side_to_short_name(side),
            x=raw_x_bounds,
            y=raw_y_bounds,
            y_offset=0.0,
            settle_ms=None,
            samples_per_point=None,
            known_shelf_edge_y=known_shelf_edge_y,
            npy=None,
            scans_dir=self._scans_dir,
            config=str(get_config_path() or CONFIG_PATH),
            apply=True,
            shelf_scout_only=False,
            rescan_cell=raw_rescan_target,
        )
        run_parameters = laser_module.build_run_parameters(
            args=args,
            side=side,
            bounds=bounds,
            step_mm=float(scan_config.fine_scan_step_mm),
            settle_ms=settle_ms,
            samples_per_point=samples_per_point,
            coarse_settle_ms=coarse_settle_ms,
            coarse_samples_per_point=coarse_samples_per_point,
            scan_config=scan_config,
        )

        if run_scope is not None:
            run_parameters["auto_calibration_scope"] = run_scope

        if rescan_target is not None and seeded_candidates is not None and len(seeded_candidates) == 1:
            candidate = seeded_candidates[0]
            candidate_wall_depth_mm = (
                float(candidate.wall_depth_mm)
                if candidate.wall_depth_mm is not None
                else None
            )
            run_parameters["single_cell_rescan"] = {
                "shelf_idx": int(rescan_target.shelf_idx),
                "cell_idx": int(rescan_target.cell_idx),
                "candidate": {
                    "x_min": float(candidate.x_min),
                    "x_max": float(candidate.x_max),
                    "y_min": float(candidate.y_min),
                    "y_max": float(candidate.y_max),
                    "wall_depth_mm": candidate_wall_depth_mm,
                },
            }

        recorder = laser_module.create_staged_scan_recorder(
            scans_dir=self._scans_dir,
            run_parameters=run_parameters,
        )
        self._side_states.setdefault(side_name, _SideState(bounds=bounds))
        await self._refresh_scan_side_state(side_name, recorder, bounds)
        logger.info(
            "laser_auto_calibration_scan_start side=%s run_dir=%s",
            side_name,
            str(recorder.run_dir),
        )
        await self._update_snapshot(
            current_side=side_name,
            current_stage="scan-start",
            state="running",
            run_id=recorder.run_id,
            bounds=bounds,
            stage_detail={
                "side": side_name,
                "phase": "scan-start",
                "run_id": recorder.run_id,
                "run_dir": str(recorder.run_dir),
            },
        )

        scan_correlation_id = get_correlation_id()

        if hasattr(worker._robot._tool, "set_motion_diagnostic_context"):
            worker._robot._tool.set_motion_diagnostic_context(
                run_id=recorder.run_id,
                correlation_id=scan_correlation_id,
            )

        try:
            async with self._patch_status_callbacks(
                scan_callback=self._build_scan_status_callback(side_name, recorder, bounds),
                angle_callback=None,
            ):
                analysis = await laser_module.run_staged_scan(
                    robot=worker._robot,
                    side=side,
                    bounds=bounds,
                    fine_step_mm=float(scan_config.fine_scan_step_mm),
                    settle_ms=settle_ms,
                    samples_per_point=samples_per_point,
                    coarse_settle_ms=coarse_settle_ms,
                    coarse_samples_per_point=coarse_samples_per_point,
                    scan_config=scan_config,
                    scans_dir=self._scans_dir,
                    run_parameters=run_parameters,
                    shelf_scout_only=False,
                    known_shelf_edge_y=known_shelf_edge_y,
                    seeded_candidates=seeded_candidates,
                    seeded_wall_depth_mm=seeded_wall_depth_mm,
                    recorder=recorder,
                    should_abort=self._should_abort_work,
                )
        finally:
            if hasattr(worker._robot._tool, "set_motion_diagnostic_context"):
                worker._robot._tool.set_motion_diagnostic_context(
                    run_id=getattr(self, "_motion_diagnostic_session_id", self._snapshot.session_id),
                    correlation_id=getattr(self, "_motion_diagnostic_correlation_id", scan_correlation_id),
                )
        await self._refresh_scan_side_state(side_name, recorder, bounds)
        await self._refresh_scan_side_state(side_name, recorder, bounds)
        await self._update_snapshot(
            current_side=side_name,
            current_stage="side-complete",
            state="running",
            run_id=analysis.run_id,
            bounds=bounds,
            stage_detail={"side": side_name, "phase": "completed", "run_id": analysis.run_id},
        )
        return analysis
