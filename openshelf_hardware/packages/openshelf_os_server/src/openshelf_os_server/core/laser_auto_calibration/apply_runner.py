# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from openshelf_os.config.laser_scan import (
    LaserScanShelfInterceptConfig,
    LaserScanXYOffsetConfig,
    LaserScanZOffsetModelConfig,
)
from openshelf_os.laser_calibration.angle.capture import enter_capture_mode
from openshelf_os.laser_calibration.defaults import Z_OFFSET_MODEL_PARAM_TOLERANCE_MM

from .models import *


_MANUAL_SHELF_MATCH_TOLERANCE_MM = 60.0
_MANUAL_CELL_MATCH_TOLERANCE_MM = 40.0


def _reference_derivation_scope(
    requested_scope: Any,
) -> tuple[int | None, int | None] | None:
    """Shelf and cell the datum is derived from in this run's scan, or None when it cannot be seen.

    Thin adapter over `select_datum_derivation_scope`: the rule lives in the package so the
    scope-validation and apply paths cannot drift apart. A full-module scan sees every opening,
    so it frames the side from all of its manual cells (None, None). A shelf or cell rescan sees
    only its own part of the side: when that part *is* the datum the run re-frames from it, and
    otherwise the scan holds nothing to measure the frame against, so the run keeps the frame the
    datum already established rather than failing a routine rescan.
    """
    if requested_scope is None:
        return (None, None)

    return laser_module.select_datum_derivation_scope(
        mode=getattr(requested_scope, "mode", None),
        shelf_idx=getattr(requested_scope, "shelf_idx", None),
        cell_idx=getattr(requested_scope, "cell_idx", None),
    )


def _reference_offset_scan_config(
	scan_config: Any,
	side: StorageModuleSide,
	result: laser_module.ReferenceOffsetResult,
) -> None:
	"""Install a derived offset on the run's config, in place.

	The apply reads the offset from the scan config, and the caller saves that same config
	after a successful run: one assignment both frames the run and persists what it derived.
	"""
	offset = LaserScanXYOffsetConfig(
		x_mm=float(result.offset_x_mm),
		y_mm=float(result.offset_y_mm),
	)

	if side == StorageModuleSide.FRONT:
		scan_config.front_gripper_xy_correction_mm = offset
	else:
		scan_config.back_gripper_xy_correction_mm = offset


def _build_reference_offset_entry(
	result: laser_module.ReferenceOffsetResult,
	*,
	applied: bool,
	persisted: bool,
) -> LaserAutoCalibrationReferenceOffset:
	return LaserAutoCalibrationReferenceOffset(
		side=result.side,
		offset_x_mm=float(result.offset_x_mm),
		offset_y_mm=float(result.offset_y_mm),
		spread_x_mm=float(result.spread_x_mm),
		spread_y_mm=float(result.spread_y_mm),
		sample_count=int(result.sample_count),
		intercept_mm=None if result.intercept_mm is None else float(result.intercept_mm),
		slope_per_mm=float(result.slope_per_mm),
		configured_offset_x_mm=result.configured_offset_x_mm,
		configured_offset_y_mm=result.configured_offset_y_mm,
		applied=applied,
		persisted=persisted,
		samples=[
			LaserAutoCalibrationReferenceOffsetSample(
				shelf_idx=int(sample.shelf_idx),
				cell_idx=int(sample.cell_idx),
				manual_x_mm=float(sample.manual_x_mm),
				manual_y_mm=float(sample.manual_y_mm),
				detected_x_mm=float(sample.detected_x_mm),
				detected_y_mm=float(sample.detected_y_mm),
				delta_x_mm=float(sample.delta_x_mm),
				delta_y_mm=float(sample.delta_y_mm),
				wall_depth_mm=sample.wall_depth_mm,
				derived_intercept_mm=sample.derived_intercept_mm,
			)
			for sample in result.samples
		],
		skipped=list(result.skipped),
	)

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
    appended_shelf = False

    for source_shelf, manual_cells in manual_groups:
        # Match on the manual cells alone: the stale auto cells of the old shelf can sit on a
        # different row and drag its center away from the opening the datum actually belongs to.
        source_y = sum(float(cell.calibration_point.y) for cell in manual_cells) / len(manual_cells)
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
            appended_shelf = True
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

    # An appended shelf still carries its old index, which can collide with a scanned one.
    if appended_shelf is True:
        result.sort(key=lambda shelf: _shelf_center_y(shelf) or float("inf"))
        for idx, shelf in enumerate(result):
            shelf.shelf_idx = idx

    return result


def _get_x_axis_limit_mm(worker: RobotWorker) -> float | None:
    """Max reachable X for the tool carriage, or None when the motor does not expose it."""
    motor = getattr(getattr(worker._robot, "_tool", None), "_mx", None)
    limit = getattr(getattr(motor, "_params", None), "lim", None)

    if limit is None:
        laser_module.print_status("cell_unreachable=skipped reason=no_x_axis_limit")
        return None

    return float(limit)


def _get_x_axis_lower_limit_mm(worker: RobotWorker) -> float | None:
    """Min reachable X for the tool carriage, or None when the motor does not expose it."""
    motor = getattr(getattr(worker._robot, "_tool", None), "_mx", None)
    limit = getattr(getattr(motor, "_params", None), "lower_lim", None)

    if limit is None:
        return None

    return float(limit)


class ApplyRunnerMixin:
    _reference_offset_entries: list[LaserAutoCalibrationReferenceOffset]

    def _resolve_manual_z_intercept(
        self,
        *,
        side: StorageModuleSide,
        manual_reference: laser_module.ReferenceOffsetResult | None,
    ) -> float:
        """The intercept the hand-calibrated cells imply, without which a probe proves nothing."""
        if manual_reference is None or manual_reference.intercept_mm is None:
            raise ValueError(
                f"z stall anchor: the {side.value} has no hand-calibrated reference cell with a laser "
                "wall depth, and the probe fit is not trustworthy without one; precalibrate a cell "
                "(cell calibration, 'keep manual') before probing"
            )

        laser_module.check_datum_stage_carry(manual_reference)

        return float(manual_reference.intercept_mm)

    async def _derive_z_model_from_grip_edge(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        scan_config: Any,
        *,
        source_grid: Any,
        manual_reference: laser_module.ReferenceOffsetResult | None,
    ) -> tuple[float, float, list[tuple[float, float]], dict[tuple[int, int], float]]:
        """Give every regular cell its own z from where the gripper starts to grip it.

        The laser sees each tray face, but where the hook-shaped fingers catch the hole shifts per
        shelf (lb4a-3: 1.2-3.9mm against the wall) and by up to 1.25mm between trays on one shelf,
        so one intercept cannot place every cell. Each cell's edge is found with the gripper's
        object detection (`run_grip_edge_sweep`) and its z is set mid-way to where the grip gives out
        deeper in; the datum check compares edge + GRIP_EDGE_TARGET_MARGIN_MM; each shelf's stored
        intercept is its deepest cell's, since the hook tolerates overshoot but not falling short,
        and places what was not measured (single-shelf rescans). The datum's own edge is checked against its hand-set z, which
        refuses the run when they disagree by more than z_stall_manual_agreement_mm. A shelf where
        no cell grips steadily (multi-item bins) keeps the datum's intercept and is reported.
        """
        # Refused before any motion: the datum is what the measured edges are checked against.
        manual_intercept_mm = self._resolve_manual_z_intercept(side=side, manual_reference=manual_reference)
        device_grid = getattr(worker._robot, "_grid", None) or source_grid
        datum_cells = laser_module.datum_probe_cells(
            grid=device_grid, side=side, analysis=analysis, scan_config=scan_config
        )
        disabled_cells = (
            laser_module.excluded_probe_cells(grid=device_grid, side=side, analysis=analysis, scan_config=scan_config)
            - datum_cells
        )
        configured_model = laser_module.get_wall_depth_to_gripper_z_model(scan_config=scan_config, side=side)
        slope_per_mm = 0.0 if configured_model is None else float(configured_model.x_slope_mm_per_mm)
        metadata_path = getattr(analysis, "metadata_path", None)
        candidates = (
            laser_module.load_candidates_from_metadata(laser_module.load_json_file(metadata_path))
            if metadata_path is not None and Path(metadata_path).exists()
            else []
        )

        def sample_intercept_mm(sample: Any) -> float:
            return float(sample.target_z_mm) - float(sample.wall_depth_mm) - slope_per_mm * float(sample.grab_x_mm)

        tolerance_mm = float(scan_config.z_stall_manual_agreement_mm)
        datum_checked = False

        def check_datum(sample: Any) -> None:
            # Runs as soon as the first datum is probed: a datum several mm off would otherwise steer
            # every later search, and a too-deep one lets the hook push trays on the other shelves.
            nonlocal datum_checked

            if datum_checked:
                return

            if sample is None:
                raise ValueError(
                    "grip edge: the datum cell gave no steady grip within its search window; re-check its "
                    "hand-set z and the gripper (open position, object detection) before retrying"
                )

            disagreement_mm = sample_intercept_mm(sample) - manual_intercept_mm

            if abs(disagreement_mm) > tolerance_mm:
                raise ValueError(
                    f"grip edge: the datum cell's grip edge puts its z {abs(disagreement_mm):.2f}mm "
                    f"{'deeper' if disagreement_mm > 0 else 'shallower'} than its hand-set z (tolerance "
                    f"{tolerance_mm:.2f}mm); re-check the datum's z or its tray before retrying"
                )

            datum_checked = True

        result = await laser_module.run_grip_edge_sweep(
            tool=worker._robot._tool,
            analysis=analysis,
            wall_depths=laser_module.wall_depths_by_cell(analysis=analysis, candidates=candidates),
            side=side,
            xy_offset=laser_module.get_gripper_xy_correction(scan_config=scan_config, side=side),
            intercept_mm=manual_intercept_mm,
            slope_per_mm=slope_per_mm,
            preferred_cells=datum_cells,
            excluded_cells=disabled_cells,
            x_axis_limit_mm=_get_x_axis_limit_mm(worker),
            x_axis_lower_limit_mm=_get_x_axis_lower_limit_mm(worker),
            should_abort=self._should_abort_work,
            check_preferred=check_datum,
        )

        if len(datum_cells) > 0 and not datum_checked:
            # The sweep never reached a datum (e.g. past the x travel): nothing anchored the edges.
            check_datum(None)

        deepest_by_shelf: dict[int, Any] = {}

        for sample in result.samples:
            current = deepest_by_shelf.get(int(sample.shelf_idx))

            if current is None or sample_intercept_mm(sample) > sample_intercept_mm(current):
                deepest_by_shelf[int(sample.shelf_idx)] = sample

        shelf_intercepts = [
            (float(sample.grab_y_mm), sample_intercept_mm(sample)) for sample in deepest_by_shelf.values()
        ]
        cell_z_offsets = {
            (int(sample.shelf_idx), int(sample.cell_idx)): float(sample.target_z_mm) for sample in result.samples
        }
        sampled_shelves = {int(sample.shelf_idx) for sample in result.samples}

        for shelf in analysis.shelves:
            if len(shelf.cells) > 0 and int(shelf.shelf_idx) not in sampled_shelves:
                laser_module.print_status(
                    f"grip_edge=shelf_unmeasured side={side.value} shelf={int(shelf.shelf_idx)} "
                    f"y_mm={float(shelf.center_y_mm):.1f} uses_intercept_mm={manual_intercept_mm:.3f}"
                )

        stage_extension_mm = float(await worker._robot._tool.get_param(_side_to_stage_extension_param_name(side)))
        z_place_mm = float(await worker._robot._tool.get_param("Z_EXTENSION_PLACE"))
        # A partial scope measures only its own shelves: the stored entries of the others stay.
        stored_shelves = (
            []
            if configured_model is None
            else laser_module.shelf_intercepts_at_stage(model=configured_model, stage_extension_mm=stage_extension_mm)
        )
        shelf_intercepts += [
            (y_mm, intercept_mm)
            for y_mm, intercept_mm in stored_shelves
            if all(abs(y_mm - measured_y_mm) > laser_module.SHELF_INTERCEPT_MATCH_MM for measured_y_mm, _ in shelf_intercepts)
        ]

        for y_mm, intercept_mm in sorted(shelf_intercepts):
            laser_module.print_status(
                f"grip_edge=shelf_intercept side={side.value} y_mm={y_mm:.1f} intercept_mm={intercept_mm:.3f}"
            )

        self._install_shelf_z_model(
            scan_config,
            side,
            intercept_mm=manual_intercept_mm,
            slope_per_mm=slope_per_mm,
            stage_extension_mm=stage_extension_mm,
            z_place_mm=z_place_mm,
            shelf_intercepts=shelf_intercepts,
        )

        return float(manual_intercept_mm), slope_per_mm, shelf_intercepts, cell_z_offsets

    def _record_reference_offset_result(
        self,
        entry: LaserAutoCalibrationReferenceOffset,
    ) -> list[LaserAutoCalibrationReferenceOffset]:
        """Replace this side's entry, so a re-derived value never reads as two results."""
        entries: list[LaserAutoCalibrationReferenceOffset] = [
            existing
            for existing in self._reference_offset_entries
            if existing.side != entry.side
        ]
        entries.append(entry)
        self._reference_offset_entries = entries

        return [stored.model_copy(deep=True) for stored in entries]

    async def _derive_reference_offset(
        self,
        *,
        side: StorageModuleSide,
        analysis: Any,
        source_grid: Any,
    ) -> laser_module.ReferenceOffsetResult | None:
        """Derive this side's xy offset from the hand-calibrated datum its scan can see.

        Returns None when the run's scope does not hold the bottom-left datum cell: the frame is
        anchored on that cell, and a scan of some other opening holds nothing to re-derive it
        from. The result is published before the apply touches anything, so a failure later still
        shows what the reference frame implied. The derived value is used and persisted by the
        caller, not here.
        """
        side_name = _side_to_module_name(side)
        derivation_scope = _reference_derivation_scope(self._snapshot.requested_scope)

        if derivation_scope is None:
            laser_module.print_status(
                f"reference_offset=skipped side={side_name} reason=datum_out_of_scan "
                f"datum={laser_module.REFERENCE_DATUM_SHELF_IDX}:{laser_module.REFERENCE_DATUM_CELL_IDX}"
            )
            return None

        shelf_idx, cell_idx = derivation_scope
        config = get_root_config()
        stage_extension_value = getattr(
            config.robot.tool.parameters, _side_to_stage_extension_param_name(side), None
        )
        # Stamped datums are carried to the stage the apply places at, so the intercept they imply
        # is the one the probe is judged against and the one a probe-less run installs.
        result = laser_module.derive_reference_offsets(
            grid=source_grid,
            analysis=analysis,
            side=side,
            scan_config=config.robot.grid.scan,
            shelf_idx=shelf_idx,
            cell_idx=cell_idx,
            stage_extension_mm=None if stage_extension_value is None else float(stage_extension_value),
        )
        laser_module.print_status(
            f"reference_offset_derivation side={side_name} "
            f"samples={result.sample_count} offset_x_mm={result.offset_x_mm:.3f} "
            f"offset_y_mm={result.offset_y_mm:.3f} spread_x_mm={result.spread_x_mm:.3f} "
            f"spread_y_mm={result.spread_y_mm:.3f} "
            f"intercept_mm={'none' if result.intercept_mm is None else f'{result.intercept_mm:.3f}'}"
        )

        if result.sample_count == 1:
            laser_module.print_status(
                "reference_offset_warning single_reference_cell "
                "(only as precise as that cell's manual placement)"
            )

        for skipped in result.skipped:
            laser_module.print_status(f"reference_offset_skipped {skipped}")

        entries = self._record_reference_offset_result(
            _build_reference_offset_entry(result, applied=False, persisted=False)
        )
        await self._update_snapshot(reference_offset_results=entries)

        return result

    async def _publish_reference_offset_applied(
        self,
        result: laser_module.ReferenceOffsetResult,
        *,
        persisted: bool,
    ) -> None:
        entries = self._record_reference_offset_result(
            _build_reference_offset_entry(result, applied=True, persisted=persisted)
        )
        await self._update_snapshot(reference_offset_results=entries)

    async def _resolve_z_delta_model(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        scan_config: Any,
        *,
        allow_missing: bool = False,
        source_grid: Any = None,
        manual_reference: laser_module.ReferenceOffsetResult | None = None,
        allow_probe: bool = True,
    ) -> tuple[float, float, list[tuple[float, float]], dict[tuple[int, int], float]] | None:
        """Resolve (intercept, slope, shelf intercepts, measured cell z) for z_offset = wall_depth +
        intercept + slope * grab_x; measured cell z (analysis keys) replaces that for its cells.

        A cell's intercept is its shelf's entry in the (grab y, intercept) list when it has one: the
        z stall probe measures every shelf, and the other paths carry what it stored.

        `z_stall_anchor_enabled` re-derives the model by probing the plate and replaces the
        configured one: a probed model is only valid for the machine it was probed on, which is
        the point of asking for it. Without the probe, a stamped hand-calibrated datum in the scan
        sets the intercept (keeping the configured slope) and is installed pinned to the live
        geometry; otherwise the configured model is carried to the live stage extension, and
        refused only when Z_EXTENSION_PLACE moved. `allow_probe` is False for a scan whose scope
        does not hold the datum the probed model is judged against, so a partial rescan keeps the
        model the machine already has.
        """
        if allow_probe is True and _z_stall_requested(scan_config) is True:
            return await self._derive_z_model_from_grip_edge(
                worker,
                side,
                analysis,
                scan_config,
                source_grid=source_grid,
                manual_reference=manual_reference,
            )

        configured_model = laser_module.get_wall_depth_to_gripper_z_model(scan_config=scan_config, side=side)
        datum_intercept_mm = laser_module.stamped_datum_intercept_mm(manual_reference)

        side_name = "front" if side == StorageModuleSide.FRONT else "back"

        if configured_model is None and datum_intercept_mm is None:
            if allow_missing is True:
                return None

            raise ValueError(
                f"no z offset source for the {side_name} module: configure "
                f"robot.grid.scan.{side_name}_wall_depth_to_gripper_z_model, extend the stage to the "
                "shelf extension in cell calibration and jog the bottom-left reference cell's z onto the "
                "tray and save it (the stage extension is recorded only when the z changes), or enable "
                "the z stall probe. "
                "Persisting without one would write z_offset=0 to every cell."
            )

        tool = worker._robot._tool
        stage_param = _side_to_stage_extension_param_name(side)
        stage_extension_value = await tool.get_param(stage_param)
        z_place_value = await tool.get_param("Z_EXTENSION_PLACE")

        if stage_extension_value is None or z_place_value is None:
            raise ValueError(f"{stage_param} and Z_EXTENSION_PLACE are required to apply the z offset model")

        stage_extension_mm = float(stage_extension_value)
        z_place_mm = float(z_place_value)

        slope_per_mm = 0.0 if configured_model is None else float(configured_model.x_slope_mm_per_mm)
        configured_intercept_mm = (
            None
            if configured_model is None
            else laser_module.z_offset_model_intercept_at_stage(
                model=configured_model,
                stage_extension_mm=stage_extension_mm,
            )
        )

        configured_shelves = (
            []
            if configured_model is None
            else laser_module.shelf_intercepts_at_stage(model=configured_model, stage_extension_mm=stage_extension_mm)
        )

        if datum_intercept_mm is not None:
            # The hand-calibrated datum is this machine's own z, so it sets the intercept; the
            # configured slope is kept because one opening cannot measure a tilt. Shelf intercepts
            # keep their offsets from the model intercept, which is the datum's on every install
            # path (a shelf entry is its deepest cell, not the datum). Installed on the run's
            # config, pinned to the live geometry, so later rescans stay consistent.
            shift_mm = 0.0 if configured_intercept_mm is None else datum_intercept_mm - configured_intercept_mm
            shelf_intercepts = [(y_mm, intercept_mm + shift_mm) for y_mm, intercept_mm in configured_shelves]
            self._install_shelf_z_model(
                scan_config,
                side,
                intercept_mm=datum_intercept_mm,
                slope_per_mm=slope_per_mm,
                stage_extension_mm=stage_extension_mm,
                z_place_mm=z_place_mm,
                shelf_intercepts=shelf_intercepts,
            )

            laser_module.print_status(
                f"z_offset_model=datum side={side_name} intercept_mm={datum_intercept_mm:.3f} "
                f"slope_per_mm={slope_per_mm:.6f} stage_extension_mm={stage_extension_mm:.2f} "
                f"z_place_mm={z_place_mm:.2f} configured_intercept_mm="
                f"{'none' if configured_intercept_mm is None else f'{configured_intercept_mm:.3f}'} "
                f"shelf_intercepts={len(shelf_intercepts)}"
            )
            return datum_intercept_mm, slope_per_mm, shelf_intercepts, {}

        assert configured_model is not None and configured_intercept_mm is not None
        laser_module.require_z_offset_model_matches_z_place(
            model=configured_model,
            side=side,
            z_place_mm=z_place_mm,
            tolerance_mm=float(Z_OFFSET_MODEL_PARAM_TOLERANCE_MM),
        )
        laser_module.print_status(
            f"z_offset_model=config side={side_name} intercept_mm={configured_intercept_mm:.3f} "
            f"slope_per_mm={slope_per_mm:.6f} stage_extension_mm={stage_extension_mm:.2f} "
            f"pinned_stage_mm={float(configured_model.reference_stage_mm):.2f} z_place_mm={z_place_mm:.2f} "
            f"shelf_intercepts={len(configured_shelves)}"
        )
        return configured_intercept_mm, slope_per_mm, configured_shelves, {}

    def _install_shelf_z_model(
        self,
        scan_config: Any,
        side: StorageModuleSide,
        *,
        intercept_mm: float,
        slope_per_mm: float,
        stage_extension_mm: float,
        z_place_mm: float,
        shelf_intercepts: list[tuple[float, float]],
    ) -> None:
        side_name = "front" if side == StorageModuleSide.FRONT else "back"
        setattr(
            scan_config,
            f"{side_name}_wall_depth_to_gripper_z_model",
            LaserScanZOffsetModelConfig(
                intercept_mm=float(intercept_mm),
                x_slope_mm_per_mm=float(slope_per_mm),
                reference_stage_mm=float(stage_extension_mm),
                reference_z_place_mm=float(z_place_mm),
                shelf_intercepts=[
                    LaserScanShelfInterceptConfig(y_mm=float(y_mm), intercept_mm=float(shelf_mm))
                    for y_mm, shelf_mm in sorted(shelf_intercepts)
                ],
            ),
        )


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
        reference_results: dict[StorageModuleSide, laser_module.ReferenceOffsetResult] = {}
        reference_persisted: dict[StorageModuleSide, bool] = {}

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
        front_module = list(source_grid.front_module)
        back_module = list(source_grid.back_module)

        for completed_result in completed_results:
            if len(completed_result.analysis.shelves) == 0:
                raise ValueError("No tray openings detected from scan")

            reference_result = await self._derive_reference_offset(
                side=completed_result.side,
                analysis=completed_result.analysis,
                source_grid=source_grid,
            )

            if reference_result is not None:
                _reference_offset_scan_config(scan_config, completed_result.side, reference_result)
                reference_results[completed_result.side] = reference_result
                reference_persisted[completed_result.side] = True

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
            z_delta_model = await self._resolve_z_delta_model(
                worker,
                completed_result.side,
                completed_result.analysis,
                scan_config,
                source_grid=source_grid,
                manual_reference=reference_result,
            )

            if z_delta_model is None:
                raise ValueError("z offset model could not be resolved for a full side rebuild")

            z_delta_intercept_mm, z_delta_slope_per_mm, z_shelf_intercepts, z_cell_offsets = z_delta_model
            laser_module.apply_wall_depth_z_offsets(
                analysis=completed_result.analysis,
                scanned_shelves=scanned_shelves,
                metadata_path=str(completed_result.analysis.metadata_path),
                z_delta_mm=z_delta_intercept_mm,
                z_delta_slope_per_mm=z_delta_slope_per_mm,
                shelf_intercepts=z_shelf_intercepts,
                cell_z_offsets=z_cell_offsets,
            )

            existing_side_shelves = (
                list(source_grid.front_module)
                if completed_result.side == StorageModuleSide.FRONT
                else list(source_grid.back_module)
            )
            scanned_shelves = _preserve_manual_cells(scanned_shelves, existing_side_shelves)

            # Reachability is a hard physical constraint, so this runs last and covers
            # manual cells too: the laser can see holes past the tool's X travel.
            x_axis_limit_mm = _get_x_axis_limit_mm(worker)
            if x_axis_limit_mm is not None:
                laser_module.disable_unreachable_cells(
                    scanned_shelves=scanned_shelves,
                    x_axis_limit_mm=x_axis_limit_mm,
                )

            if completed_result.side == StorageModuleSide.FRONT:
                front_module = scanned_shelves
            else:
                back_module = scanned_shelves

        storage_grid = laser_module.StorageGrid(
            front_module=front_module,
            back_module=back_module,
            filename=source_grid.filename if source_grid.filename != "" else str(grid_path),
        )

        version_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        try:
            storage_grid.save(version_id=version_id)
            save_root_config(next_config, version_id=version_id)
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

        for completed_result in completed_results:
            if reference_results.get(completed_result.side) is not None:
                await self._publish_reference_offset_applied(
                    reference_results[completed_result.side],
                    persisted=reference_persisted.get(completed_result.side, False),
                )

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
        grid_path = Path(resolve_grid_path(config))
        grid = laser_module.load_grid(grid_path)
        reference_result = await self._derive_reference_offset(
            side=side,
            analysis=analysis,
            source_grid=grid,
        )
        # The probe's model is judged against the datum, so a rescan whose scope does not hold the
        # datum cannot fit one: it keeps whatever model the machine already has.
        datum_in_scan = _reference_derivation_scope(self._snapshot.requested_scope) is not None
        z_stall_requested = _z_stall_requested(config.robot.grid.scan) and datum_in_scan

        # Both derivations write to the config copy, and that copy is what gets saved: editing the
        # live config in place would leave the probe's model unpersisted if the apply then failed.
        apply_config = config

        if reference_result is not None or z_stall_requested is True:
            apply_config = config.model_copy(deep=True)

            if reference_result is not None:
                _reference_offset_scan_config(apply_config.robot.grid.scan, side, reference_result)

        # A single-shelf rescan may legitimately run with no z source at all: apply_single_shelf_analysis
        # then leaves the shelf's existing z offsets alone rather than rebuilding them.
        z_delta_model = await self._resolve_z_delta_model(
            worker,
            side,
            analysis,
            apply_config.robot.grid.scan,
            allow_missing=True,
            source_grid=grid,
            manual_reference=reference_result,
            allow_probe=datum_in_scan,
        )

        laser_module.apply_single_shelf_analysis(
            analysis=analysis,
            grid=grid,
            grid_path=grid_path,
            scan_config=apply_config.robot.grid.scan,
            side=side,
            target_shelf_idx=shelf_idx,
            z_delta_override_mm=None if z_delta_model is None else z_delta_model[0],
            z_delta_slope_per_mm=0.0 if z_delta_model is None else z_delta_model[1],
            shelf_intercepts=None if z_delta_model is None else z_delta_model[2],
            cell_z_offsets=None if z_delta_model is None else z_delta_model[3],
        )

        if apply_config is not config:
            save_root_config(apply_config)
            clear_config_cache()

        if reference_result is not None:
            await self._publish_reference_offset_applied(reference_result, persisted=True)

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

        reference_result = await self._derive_reference_offset(
            side=side,
            analysis=analysis,
            source_grid=grid,
        )
        # A run scoped to some other opening cannot see the datum, so neither the frame nor a
        # probed model can be re-derived from it.
        datum_in_scan = _reference_derivation_scope(self._snapshot.requested_scope) is not None
        z_stall_requested = _z_stall_requested(scan_config) and datum_in_scan
        next_config = config

        if reference_result is not None or z_stall_requested is True:
            next_config = config.model_copy(deep=True)

        if reference_result is not None:
            # The scanned cell *is* the reference: rewriting it from its own detection would
            # replace the operator's datum with the derived frame it just defined.
            laser_module.print_status(
                f"single_cell_apply=skipped reason=manual_reference_cell "
                f"cell={int(target.shelf_idx)}:{int(target.cell_idx)}"
            )
            _reference_offset_scan_config(next_config.robot.grid.scan, side, reference_result)
        else:
            laser_module.apply_single_cell_rescan(
                grid=grid,
                target=target,
                side=side,
                detected_cell=detected_cell,
                scan_config=next_config.robot.grid.scan,
            )

        if z_stall_requested is True:
            # One opening is one intercept: this verifies the cell's own datum by contact, and
            # keeps whatever slope the machine already has (resolve_probed_slope decides that).
            await self._resolve_z_delta_model(
                worker,
                side,
                analysis,
                next_config.robot.grid.scan,
                source_grid=grid,
                manual_reference=reference_result,
            )

        if next_config is not config:
            save_root_config(next_config)
            clear_config_cache()

            if reference_result is not None:
                await self._publish_reference_offset_applied(reference_result, persisted=True)

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


    def _persist_tool_parameter(self, worker: RobotWorker, param_name: str, value: Any) -> None:
        """Commit one calibrated tool parameter to disk as soon as it is measured.

        Angle and stage extension are self-contained results. Keeping them in memory until the
        whole run succeeds means any later failure (a scan, or the z stall anchor) discards them
        and silently leaves the tool on a stale value.
        """
        config = get_root_config()
        next_config = config.model_copy(deep=True)
        params_dict = next_config.robot.tool.parameters.model_dump()
        params_dict[param_name] = value
        next_config.robot.tool.parameters = Parameters.model_validate(params_dict)

        save_root_config(next_config)
        clear_config_cache()
        worker._robot.reload_parameters()


    def _restore_in_memory_calibration_params(self, worker: RobotWorker, params: dict[str, float]) -> None:
        """Drop uncommitted in-memory parameter edits by reloading what is on disk.

        Calibrated parameters are persisted the moment they are measured, so disk already holds
        everything this run legitimately produced. Re-applying the pre-run snapshot would throw
        those away; only fall back to it if the reload itself fails.
        """
        try:
            clear_config_cache()
            worker._robot.reload_parameters()
            return
        except Exception:
            logger.error("laser_auto_calibration_param_reload_failed", exc_info=True)

        params_dict = worker._robot._tool._params.parameters.model_dump()
        params_dict["ROTATION_FRONT"] = float(params["ROTATION_FRONT"])
        params_dict["ROTATION_BACK"] = float(params["ROTATION_BACK"])
        params_dict["STAGE_EXTENSION_SHELF_FRONT"] = float(params["STAGE_EXTENSION_SHELF_FRONT"])
        params_dict["STAGE_EXTENSION_SHELF_BACK"] = float(params["STAGE_EXTENSION_SHELF_BACK"])
        worker._robot._tool._params.parameters = Parameters.model_validate(params_dict)
