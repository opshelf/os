# ruff: noqa: F401, F403, F405
# mypy: disable-error-code=attr-defined
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

from openshelf_os.common import AxisCoordinate
from openshelf_os.storage import StorageModuleCellLocation

from .models import *

logger = logging.getLogger(__name__)


def _write_gripper_debug_document(result: LaserAutoCalibrationGripperResult) -> None:
    timestamp = _get_timestamp().replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    output_path = Path(_build_default_scans_dir()) / ANGLE_DEBUG_DIR_NAME / f"gripper-open-position-{timestamp}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result.model_dump(mode="json"), indent="\t"), encoding="utf-8")


def _persist_gripper_open_position(worker: RobotWorker, open_position: int) -> None:
    config = get_root_config()
    next_config = config.model_copy(deep=True)
    next_config.robot.tool.gripper.open_position = int(open_position)
    save_root_config(next_config)
    clear_config_cache()
    worker._robot.reload_parameters()


async def calibrate_gripper_open_position(
    worker: RobotWorker,
    sides: list[StorageModuleSide],
    *,
    should_abort: Callable[[], bool] | None = None,
) -> LaserAutoCalibrationGripperResult:
    """Set robot.tool.gripper.open_position from where each side's hand-calibrated reference tray
    stops the fingers splayed fully open, at that cell's own z.

    open_position is one drive register for both sides, so the side whose hole stops the fingers
    widest sets it. The new value goes live, is checked with one more splay per side, and is only
    then persisted; a side without a steady grip (an empty cell, a multi-item bin) is skipped, and
    without any measurement or a failed check the drive keeps its previous value. When a side that
    has a reference cell was not measured, the value only moves more open than the previous one.
    """
    tool = worker._robot._tool
    grid = worker._robot._grid
    previous = int(tool._gs.open_position)
    result = LaserAutoCalibrationGripperResult(previous_open_position=previous)
    measured: list[tuple[LaserAutoCalibrationGripperSample, AxisCoordinate]] = []

    for side in sides:
        side_name = _side_to_short_name(side)
        datum = None if grid is None else laser_module.select_reference_datum_cell(grid=grid, side=side)

        if datum is None:
            result.skipped.append(f"{side_name}: no hand-calibrated reference cell")
            continue

        shelf_idx, cell = datum
        location = StorageModuleCellLocation(side=side, shelf_idx=int(shelf_idx), cell_idx=int(cell.cell_idx))
        point = grid.get_cell_coordinate(location)
        coordinate = AxisCoordinate(
            x=point.x, y=point.y, s=0 if side == StorageModuleSide.FRONT else 1, z_offset=point.z_offset
        )
        measurement = await laser_module.measure_hole_stop(tool, coordinate, should_abort=should_abort)
        sample = LaserAutoCalibrationGripperSample(
            side=side,
            shelf_idx=location.shelf_idx,
            cell_idx=location.cell_idx,
            z_offset_mm=float(coordinate.z_offset),
            positions=measurement.positions,
            states=measurement.states,
            stop_position=measurement.stop_position,
        )
        result.samples.append(sample)
        laser_module.print_status(
            f"phase=gripper-open-position side={side_name} cell={location.shelf_idx}:{location.cell_idx} "
            f"z_mm={coordinate.z_offset:.2f} positions={measurement.positions} stop={measurement.stop_position}"
        )

        if measurement.stop_position is None:
            result.skipped.append(f"{side_name}: no steady grip at the reference cell")
            continue

        measured.append((sample, coordinate))

    open_position = None

    if len(measured) > 0:
        open_position = laser_module.open_position_for_stop(min(int(sample.stop_position) for sample, _ in measured))
        measured_sides = {sample.side for sample, _ in measured}
        unmeasured = [
            _side_to_short_name(side)
            for side in (StorageModuleSide.FRONT, StorageModuleSide.BACK)
            if side not in measured_sides and laser_module.select_reference_datum_cell(grid=grid, side=side) is not None
        ]

        if unmeasured and open_position >= previous:
            # An unmeasured side may need the widest hole's value from an earlier run: only ever move more open.
            result.open_position = previous
            result.skipped.append(
                f"open position stays 0x{previous:02X}: {', '.join(unmeasured)} not measured, "
                f"measured value 0x{open_position:02X} would be narrower"
            )
            laser_module.print_status(
                f"phase=gripper-open-position open_position=0x{previous:02X} kept (measured 0x{open_position:02X}, "
                f"unmeasured={','.join(unmeasured)})"
            )
            open_position = None

    if open_position is not None:
        result.open_position = open_position
        verified = False

        try:
            await tool._gs.set_open_position(open_position)

            for sample, coordinate in measured:
                if should_abort is not None and should_abort():
                    raise asyncio.CancelledError()

                state = await laser_module.probe_cell(tool, coordinate)
                sample.verify_state = str(state)
                sample.verified = bool(state.gripped)

            verified = all(sample.verified for sample, _ in measured)
        finally:
            if not verified:
                await tool._gs.set_open_position(previous)

        if verified:
            _persist_gripper_open_position(worker, open_position)
            result.applied = True
        else:
            result.skipped.append(f"open position 0x{open_position:02X} gave no grip on the check splay")

        laser_module.print_status(
            f"phase=gripper-open-position previous=0x{previous:02X} open_position=0x{open_position:02X} "
            f"applied={result.applied}"
        )

    logger.info("gripper_open_position_calibration", extra={"result": result.model_dump(mode="json")})

    try:
        _write_gripper_debug_document(result)
    except OSError as exc:
        logger.warning("gripper_open_position_debug_write_failed", exc_info=exc)

    return result


class GripperRunnerMixin:
    async def _run_gripper_open_position_calibration(
        self,
        worker: RobotWorker,
        sides: list[StorageModuleSide],
    ) -> None:
        await self._update_snapshot(current_stage="gripper-open-position", state="running")
        result = await calibrate_gripper_open_position(worker, sides, should_abort=self._should_abort_work)
        await self._update_snapshot(gripper_open_position=result)
