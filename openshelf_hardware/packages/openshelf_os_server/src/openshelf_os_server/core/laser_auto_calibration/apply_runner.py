# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from openshelf_os.config.laser_scan import LaserScanXYOffsetConfig, LaserScanZOffsetModelConfig
from openshelf_os.laser_calibration.angle.capture import enter_capture_mode
from openshelf_os.laser_calibration.angle.robot_ops import probe_z_until_stall

from .models import *


_MANUAL_SHELF_MATCH_TOLERANCE_MM = 60.0
_MANUAL_CELL_MATCH_TOLERANCE_MM = 40.0


def _z_stall_requested(scan_config: Any, requested_scope: Any = None) -> bool:
    """`z_stall_anchor_enabled` is the machine's setting; the scope flag is this run's request."""
    if getattr(scan_config, "z_stall_anchor_enabled", False) is True:
        return True

    return getattr(requested_scope, "derive_z_model_from_stall", False) is True


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

	if side == StorageModuleSide.LEFT:
		scan_config.left_xy_offset = offset
	else:
		scan_config.right_xy_offset = offset


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


def _get_z_axis_limit_mm(worker: RobotWorker) -> float | None:
    """Max reachable Z for the gripper, or None when the motor does not expose it."""
    motor = getattr(getattr(worker._robot, "_tool", None), "_mg", None)
    limit = getattr(getattr(motor, "_params", None), "lim", None)

    if limit is None:
        return None

    return float(limit)


class ApplyRunnerMixin:
    _reference_offset_entries: list[LaserAutoCalibrationReferenceOffset]
    _z_stall_entries: list[LaserAutoCalibrationZStallResult]

    def _record_z_stall_result(
        self,
        entry: LaserAutoCalibrationZStallResult,
    ) -> list[LaserAutoCalibrationZStallResult]:
        entries: list[LaserAutoCalibrationZStallResult] = [
            existing
            for existing in self._z_stall_entries
            if existing.side != entry.side
        ]
        entries.append(entry)
        self._z_stall_entries = entries

        return [stored.model_copy(deep=True) for stored in entries]

    def _install_z_offset_model(
        self,
        scan_config: Any,
        result: laser_module.ZStallResult,
        *,
        slope_per_mm: float | None = None,
    ) -> None:
        """Write the probed model onto the run's config, pinned to the geometry it was probed at."""
        model = LaserScanZOffsetModelConfig(
            intercept_mm=float(result.intercept_mm),
            slope_per_mm=float(result.slope_per_mm if slope_per_mm is None else slope_per_mm),
            stage_extension_mm=float(result.stage_extension_mm),
            z_place_mm=float(result.z_place_mm),
        )

        if result.side == StorageModuleSide.LEFT:
            scan_config.left_z_offset_model = model
        else:
            scan_config.right_z_offset_model = model

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

        return float(manual_reference.intercept_mm)

    def _z_stall_datum_agreement_mm(
        self,
        *,
        side: StorageModuleSide,
        result: laser_module.ZStallResult,
        manual_reference: laser_module.ReferenceOffsetResult | None,
        manual_intercept_mm: float | None,
        slope_per_mm: float,
        scan_config: Any,
    ) -> float | None:
        """How far the model about to be installed misses the hand-calibrated datum, in mm.

        The datum intercept was derived with the slope that was configured, so comparing
        intercepts alone would let a probe-fitted tilt move the model away from the datum
        unchecked: what has to agree is where the two models PUT the datum cells, i.e.
        `intercept + slope * grab_x` at each reference sample. The worst cell is what the gate
        judges, and the same number is what the operator sees.
        """
        if manual_reference is None or manual_intercept_mm is None:
            return None

        configured_model = laser_module.get_scan_z_offset_model(scan_config=scan_config, side=side)
        configured_slope_mm = 0.0 if configured_model is None else float(configured_model.slope_per_mm)
        samples = list(manual_reference.samples)

        if len(samples) == 0:
            return abs(float(result.intercept_mm) - float(manual_intercept_mm))

        residuals = [
            abs(
                (float(result.intercept_mm) + float(slope_per_mm) * float(sample.manual_x_mm))
                - (
                    float(sample.derived_intercept_mm)
                    + configured_slope_mm * float(sample.manual_x_mm)
                )
            )
            for sample in samples
            if sample.derived_intercept_mm is not None
        ]

        if len(residuals) == 0:
            return abs(float(result.intercept_mm) - float(manual_intercept_mm))

        return max(residuals)

    def _validate_z_stall_against_manual_datum(
        self,
        *,
        side: StorageModuleSide,
        result: laser_module.ZStallResult,
        manual_reference: laser_module.ReferenceOffsetResult,
        manual_intercept_mm: float,
        slope_per_mm: float,
        scan_config: Any,
    ) -> None:
        """Refuse a probed model the hand-calibrated datum does not corroborate.

        The probe presses the shelf plate, so it measures the rack's stiffness as much as the
        tool's geometry, while the manual datum is the one z value a person stood behind. When
        the two disagree the rack moved under load or the datum was saved with the stage
        somewhere else - both need looking at rather than modelling over.
        """
        side_name = "left" if side == StorageModuleSide.LEFT else "right"
        tolerance_mm = float(scan_config.z_stall_manual_agreement_mm)
        agreement_mm = self._z_stall_datum_agreement_mm(
            side=side,
            result=result,
            manual_reference=manual_reference,
            manual_intercept_mm=manual_intercept_mm,
            slope_per_mm=slope_per_mm,
            scan_config=scan_config,
        )
        agreement_mm = 0.0 if agreement_mm is None else agreement_mm
        laser_module.print_status(
            f"z_stall_anchor=manual_check side={side_name} probe_intercept_mm={result.intercept_mm:.3f} "
            f"manual_intercept_mm={manual_intercept_mm:.3f} agreement_mm={agreement_mm:.3f} "
            f"tolerance_mm={tolerance_mm:.3f} manual_cells={manual_reference.sample_count}"
        )

        if agreement_mm > tolerance_mm:
            direction = "deeper" if result.intercept_mm > manual_intercept_mm else "shallower"
            raise ValueError(
                f"z stall anchor: the probe fit is {agreement_mm:.2f}mm {direction} than the "
                f"hand-calibrated {side.value} datum ({result.intercept_mm:.3f} vs {manual_intercept_mm:.3f}, "
                f"tolerance {tolerance_mm:.2f}mm). Probe contact deeper than the datum means the shelf "
                "moved under load; shallower means the tip met something in front of the plate. "
                "Neither is a z model - check the reference cell and the shelf before retrying"
            )

        captured_stage_mm = manual_reference.z_captured_stage_mm
        pin_tolerance_mm = float(scan_config.z_offset_model_param_tolerance_mm)

        if captured_stage_mm is not None and abs(float(captured_stage_mm) - float(result.stage_extension_mm)) > pin_tolerance_mm:
            raise ValueError(
                f"z stall anchor: the hand-calibrated reference cell was saved with the stage at "
                f"{float(captured_stage_mm):.2f}mm but the probe ran at {float(result.stage_extension_mm):.2f}mm, "
                "so its z offset is not comparable; re-save the reference cell with the stage at the "
                "shelf extension (or fix the tool parameter) and probe again"
            )

        if manual_reference.z_capture_conditions_missing > 0:
            logger.warning(
                "z_stall_reference_capture_conditions_missing",
                extra={
                    "side": side.value,
                    "missing": manual_reference.z_capture_conditions_missing,
                    "samples": manual_reference.sample_count,
                    "stage_extension_mm": float(result.stage_extension_mm),
                },
            )
            laser_module.print_status(
                f"z_stall_anchor=warning side={side_name} "
                f"unstamped_reference_cells={manual_reference.z_capture_conditions_missing} "
                "(saved before the capture stage was recorded; the stage extension could not be checked)"
            )

    async def _publish_z_stall_result(
        self,
        *,
        result: laser_module.ZStallResult,
        manual_intercept_mm: float | None,
        manual_reference: laser_module.ReferenceOffsetResult | None,
        scan_config: Any,
        slope_per_mm: float,
        slope_source: str,
        applied: bool,
        persisted: bool,
    ) -> None:
        agreement_mm = self._z_stall_datum_agreement_mm(
            side=result.side,
            result=result,
            manual_reference=manual_reference,
            manual_intercept_mm=manual_intercept_mm,
            slope_per_mm=slope_per_mm,
            scan_config=scan_config,
        )
        entry = LaserAutoCalibrationZStallResult(
            side=result.side,
            intercept_mm=float(result.intercept_mm),
            slope_per_mm=float(slope_per_mm),
            slope_source=slope_source,
            fit_source=str(result.fit_source),
            sample_count=int(result.sample_count),
            x_span_mm=float(result.x_span_mm),
            stage_extension_mm=float(result.stage_extension_mm),
            z_place_mm=float(result.z_place_mm),
            manual_intercept_mm=manual_intercept_mm,
            manual_agreement_mm=agreement_mm,
            manual_agreement_tolerance_mm=float(scan_config.z_stall_manual_agreement_mm),
            manual_captured_stage_mm=(
                None if manual_reference is None else manual_reference.z_captured_stage_mm
            ),
            manual_capture_unstamped=(
                0 if manual_reference is None else manual_reference.z_capture_conditions_missing
            ),
            applied=applied,
            persisted=persisted,
            samples=[
                LaserAutoCalibrationZStallSample(
                    shelf_idx=int(sample.shelf_idx),
                    cell_idx=int(sample.cell_idx),
                    probe_x_mm=float(sample.probe_x_mm),
                    probe_y_mm=float(sample.probe_y_mm),
                    wall_depth_mm=float(sample.wall_depth_mm),
                    z_contact_mm=float(sample.z_contact_mm),
                    z_contact_repeat_mm=sample.z_contact_repeat_mm,
                    delta_mm=float(sample.delta_mm),
                )
                for sample in result.samples
            ],
            skipped=list(result.skipped),
            dropped_repeats=list(result.dropped_repeats),
        )
        entries = self._record_z_stall_result(entry)
        await self._update_snapshot(z_stall_results=entries)

    async def _derive_z_model_from_stall(
        self,
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        scan_config: Any,
        *,
        source_grid: Any,
        manual_reference: laser_module.ReferenceOffsetResult | None,
    ) -> tuple[float, float]:
        """Probe the plate for this side's z model, validated against the hand-calibrated datum."""
        device_grid = getattr(worker._robot, "_grid", None) or source_grid
        # The probe can only judge the datum against what the scope actually detected: a run
        # scoped to the manual cell holds nothing but the datum, and the probe of that datum is
        # the point of the run.
        scanned_cells = {
            (int(cell.shelf_idx), int(cell.cell_idx))
            for shelf in analysis.shelves
            for cell in shelf.cells
        }
        result = await laser_module.run_z_stall_anchor(
            tool=worker._robot._tool,
            analysis=analysis,
            scan_config=scan_config,
            side=side,
            excluded_cells=laser_module.excluded_probe_cells(
                grid=device_grid, side=side, scanned_cells=scanned_cells
            ),
            x_axis_limit_mm=_get_x_axis_limit_mm(worker),
            x_axis_lower_limit_mm=_get_x_axis_lower_limit_mm(worker),
            z_axis_limit_mm=_get_z_axis_limit_mm(worker),
            xy_offset=laser_module.get_scan_xy_offset(scan_config=scan_config, side=side),
            should_abort=self._should_abort_work,
        )
        try:
            manual_intercept_mm = self._resolve_manual_z_intercept(
                side=side, manual_reference=manual_reference
            )
        except ValueError:
            # The probe has already moved, so the measurement must still reach the operator even
            # though it cannot be judged: publish the raw fit, then let the refusal out.
            await self._publish_z_stall_result(
                result=result,
                manual_intercept_mm=None,
                manual_reference=manual_reference,
                scan_config=scan_config,
                slope_per_mm=float(result.slope_per_mm),
                slope_source="probe",
                applied=False,
                persisted=False,
            )
            raise
        # The slope is decided before anything is judged or published: the datum comparison has to
        # use the slope that will be installed, and the operator's agreement number has to be the
        # one the gate used.
        slope_per_mm, slope_source = self._resolve_probed_slope(
            side=side, result=result, scan_config=scan_config
        )
        # Publish the raw fit before judging it: a refusal is exactly when the operator needs to
        # see both numbers side by side.
        await self._publish_z_stall_result(
            result=result,
            manual_intercept_mm=manual_intercept_mm,
            manual_reference=manual_reference,
            scan_config=scan_config,
            slope_per_mm=slope_per_mm,
            slope_source=slope_source,
            applied=False,
            persisted=False,
        )
        self._validate_z_stall_against_manual_datum(
            side=side,
            result=result,
            manual_reference=manual_reference,
            manual_intercept_mm=manual_intercept_mm,
            slope_per_mm=slope_per_mm,
            scan_config=scan_config,
        )
        # Installing on the run's config is what persists it: the callers save that same object.
        self._install_z_offset_model(scan_config, result, slope_per_mm=slope_per_mm)
        await self._publish_z_stall_result(
            result=result,
            manual_intercept_mm=manual_intercept_mm,
            manual_reference=manual_reference,
            scan_config=scan_config,
            slope_per_mm=slope_per_mm,
            slope_source=slope_source,
            applied=True,
            persisted=True,
        )

        return float(result.intercept_mm), slope_per_mm

    def _resolve_probed_slope(
        self,
        *,
        side: StorageModuleSide,
        result: laser_module.ZStallResult,
        scan_config: Any,
    ) -> tuple[float, str]:
        """Keep the configured tilt when the probe could not span the shelf to measure one.

        A probe that only reached one or two openings yields a tilt-free fit by construction, and
        writing that as the model would throw away a slope the machine already had for the sake
        of a short probe. A tilt the probe *did* measure is always taken over the configured one.
        """
        if float(result.slope_per_mm) != 0.0:
            return float(result.slope_per_mm), "probe"

        configured_model = laser_module.get_scan_z_offset_model(scan_config=scan_config, side=side)

        if configured_model is None or float(configured_model.slope_per_mm) == 0.0:
            return 0.0, "probe_constant"

        if float(result.x_span_mm) >= laser_module.Z_DELTA_TILT_MIN_X_SPAN_MM:
            return 0.0, "probe_constant"

        laser_module.print_status(
            f"z_stall_anchor=slope_from_config side={_side_to_module_name(side)} "
            f"x_span_mm={float(result.x_span_mm):.1f} "
            f"slope_per_mm={float(configured_model.slope_per_mm):.5f} "
            f"(probe span below {laser_module.Z_DELTA_TILT_MIN_X_SPAN_MM:.0f}mm, no tilt measured)"
        )

        return float(configured_model.slope_per_mm), "config_short_span"

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
        worker: RobotWorker,
        side: StorageModuleSide,
        analysis: Any,
        source_grid: Any,
        shelf_idx: int | None = None,
        cell_idx: int | None = None,
        force: bool = False,
    ) -> laser_module.ReferenceOffsetResult | None:
        """Derive this side's xy offset from the manually calibrated cells it references.

        Returns None when the run was not asked to derive one. The result is published before
        the apply touches anything, so a failure later still shows what the reference frame
        implied. The derived value is used and persisted by the caller, not here.

        `force` covers the z probe, which needs the same hand-calibrated datum to validate its
        fit even when the run is not re-framing xy.
        """
        requested_scope = self._snapshot.requested_scope

        if force is not True and requested_scope.derive_offsets_from_manual_cells is not True:
            return None

        result = laser_module.derive_reference_offsets(
            grid=source_grid,
            analysis=analysis,
            side=side,
            scan_config=get_root_config().robot.grid.scan,
            shelf_idx=shelf_idx,
            cell_idx=cell_idx,
        )
        laser_module.print_status(
            f"reference_offset_derivation side={_side_to_module_name(side)} "
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
    ) -> tuple[float, float] | None:
        """Resolve (intercept, slope) for z_offset = wall_depth + intercept + slope * grab_x.

        `z_stall_anchor_enabled` re-derives the model by probing the plate and replaces the
        configured one: a probed model is only valid for the machine it was probed on, which is
        the point of asking for it. Otherwise the configured model is used as-is once its pin
        still matches the live tool geometry.
        """
        if _z_stall_requested(scan_config, self._snapshot.requested_scope) is True:
            return await self._derive_z_model_from_stall(
                worker,
                side,
                analysis,
                scan_config,
                source_grid=source_grid,
                manual_reference=manual_reference,
            )

        configured_model = laser_module.get_scan_z_offset_model(scan_config=scan_config, side=side)

        if configured_model is not None:
            tool = worker._robot._tool
            stage_param = (
                "STAGE_EXTENSION_SHELF_FRONT" if side == StorageModuleSide.LEFT else "STAGE_EXTENSION_SHELF_BACK"
            )
            stage_extension_value = await tool.get_param(stage_param)
            z_place_value = await tool.get_param("Z_EXTENSION_PLACE")

            if stage_extension_value is None or z_place_value is None:
                raise ValueError(f"{stage_param} and Z_EXTENSION_PLACE are required to apply the z offset model")

            laser_module.require_z_offset_model_matches_tool(
                model=configured_model,
                side=side,
                stage_extension_mm=float(stage_extension_value),
                z_place_mm=float(z_place_value),
                tolerance_mm=float(scan_config.z_offset_model_param_tolerance_mm),
            )

            intercept_mm = float(configured_model.intercept_mm)
            slope_per_mm = float(configured_model.slope_per_mm)
            laser_module.print_status(
                f"z_offset_model=config side={'left' if side == StorageModuleSide.LEFT else 'right'} "
                f"intercept_mm={intercept_mm:.3f} slope_per_mm={slope_per_mm:.6f} "
                f"stage_extension_mm={float(stage_extension_value):.2f} z_place_mm={float(z_place_value):.2f}"
            )
            return intercept_mm, slope_per_mm

        if allow_missing is True:
            return None

        side_name = "left" if side == StorageModuleSide.LEFT else "right"
        raise ValueError(
            f"no z offset source for the {side_name} module: configure "
            f"robot.grid.scan.{side_name}_z_offset_model, or enable robot.grid.scan.z_stall_anchor_enabled. "
            "Persisting without one would write z_offset=0 to every cell."
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
        requested_scope = self._snapshot.requested_scope
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
        left_module = list(source_grid.left_module)
        right_module = list(source_grid.right_module)

        for completed_result in completed_results:
            if len(completed_result.analysis.shelves) == 0:
                raise ValueError("No tray openings detected from scan")

            reference_result = await self._derive_reference_offset(
                worker=worker,
                side=completed_result.side,
                analysis=completed_result.analysis,
                source_grid=source_grid,
            )
            z_manual_reference = reference_result

            if z_manual_reference is None and _z_stall_requested(scan_config, requested_scope) is True:
                z_manual_reference = await self._derive_reference_offset(
                    worker=worker,
                    side=completed_result.side,
                    analysis=completed_result.analysis,
                    source_grid=source_grid,
                    force=True,
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
                manual_reference=z_manual_reference,
            )

            if z_delta_model is None:
                raise ValueError("z offset model could not be resolved for a full side rebuild")

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

            # Reachability is a hard physical constraint, so this runs last and covers
            # manual cells too: the laser can see holes past the tool's X travel.
            x_axis_limit_mm = _get_x_axis_limit_mm(worker)
            if x_axis_limit_mm is not None:
                laser_module.disable_unreachable_cells(
                    scanned_shelves=scanned_shelves,
                    x_axis_limit_mm=x_axis_limit_mm,
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
            worker=worker,
            side=side,
            analysis=analysis,
            source_grid=grid,
            shelf_idx=shelf_idx,
        )
        z_stall_requested = _z_stall_requested(
            config.robot.grid.scan, self._snapshot.requested_scope
        )
        z_manual_reference = reference_result

        if z_manual_reference is None and z_stall_requested is True:
            z_manual_reference = await self._derive_reference_offset(
                worker=worker,
                side=side,
                analysis=analysis,
                source_grid=grid,
                shelf_idx=shelf_idx,
                force=True,
            )

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
            manual_reference=z_manual_reference,
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
            worker=worker,
            side=side,
            analysis=analysis,
            source_grid=grid,
            cell_idx=int(target.cell_idx),
        )
        requested_scope = self._snapshot.requested_scope
        z_stall_requested = _z_stall_requested(scan_config, requested_scope)
        next_config = config
        z_manual_reference = reference_result

        if z_manual_reference is None and z_stall_requested is True:
            z_manual_reference = await self._derive_reference_offset(
                worker=worker,
                side=side,
                analysis=analysis,
                source_grid=grid,
                cell_idx=int(target.cell_idx),
                force=True,
            )

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
                manual_reference=z_manual_reference,
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
