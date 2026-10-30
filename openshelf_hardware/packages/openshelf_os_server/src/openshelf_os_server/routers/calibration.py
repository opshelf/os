from __future__ import annotations
from openshelf_os_server.documentation import BooleanSuccess, response_docs, stream_docs
from fastapi.responses import Response

import asyncio
import functools
import logging
import re
from datetime import datetime, timezone
from uuid import uuid4
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, Optional, ParamSpec, TypeVar
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from openshelf_os_server.sse import EventSourceResponse

from openshelf_os.common import AxisCoordinate
from openshelf_os.common import Parameters
from openshelf_os.tool import MAX_PICK_LIFT_MM, require_grip
from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellItem,
	CellStatus,
	Dimensions,
	Shelf,
	StorageModuleCellLocation,
	StorageModuleSide,
)
from openshelf_os.laser_calibration.grip_edge import probe_grip_at
from openshelf_os.exceptions import ConfigError, GripVerificationError, IOBoxError, MotorError, MovementError, ToolError
from hardware.exceptions import MotorError as HardwareMotorError

from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.core.laser_auto_calibration import (
	LaserAutoCalibrationScope,
	LaserAutoCalibrationSnapshot,
	get_laser_auto_calibration_manager,
)
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.core.bin_storage import is_reserved_bin_location, reserved_bin_locations
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.errors import error_detail, internal_error_detail
from openshelf_os_server.routers.auth_deps import get_current_user_for_stream, require_permissions_for_stream
from openshelf_os_server.core.storage_module import side_to_s
from openshelf_os_server.settings import get_root_config, save_root_config

from openshelf_os.storage import StorageGrid

class DoorMoveResponse(BaseModel):
    success: bool
    position: Literal["close", "open"]
    position_mm: float


class StageMoveResponse(BaseModel):
    position: float = Field(description="Applied target in millimeters")
    warnings: list[str] | None


logger = logging.getLogger(__name__)

# Router-level auth gates every route here, so it cannot be scoped per route, and
# /laser/stream answers with an EventSourceResponse that stays open for the whole
# calibration session. Auth with a session that closes before the body streams
# (get_current_user would pin a pooled connection until the stream ends).
router = APIRouter(
	prefix="/api/calibration", tags=["calibration"], dependencies=[Depends(get_current_user_for_stream)]
)

# Reading calibration state needs only a login; every route that moves hardware,
# starts or aborts a run, or saves calibration also needs robot:control, which the
# default viewer role lacks. Stream-safe for the same reason as the router gate.
ROBOT_CONTROL = Depends(require_permissions_for_stream("robot:control"))

_MOTOR_ID_RE = re.compile(r"motor\s+(\d+)", re.IGNORECASE)
P = ParamSpec("P")
T = TypeVar("T")

_calibration_state_lock = asyncio.Lock()
QUICK_LIFT_HOLD_SECONDS = 1.0


def serialize_calibration_state(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    @functools.wraps(function)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        async with _calibration_state_lock:
            return await function(*args, **kwargs)

    return wrapper



async def ensure_laser_auto_calibration_not_running() -> None:
	manager = get_laser_auto_calibration_manager()

	if await manager.is_active() is False:
		return

	raise HTTPException(
		status_code=status.HTTP_409_CONFLICT,
		detail="The robot is calibrating.",
	)


async def get_robot_worker_or_503():
	try:
		return await get_robot_worker()
	except ValueError as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from exc
_DEFAULT_CELL_HEIGHT = 200.0
_DEFAULT_CELL_WIDTH = 480.0
_DEFAULT_DIVIDER_WIDTH = 12.0
_DEFAULT_CELL_LENGTH = 400.0


class PositionResponse(BaseModel):
	x: float = Field(description="Position or extension in millimeters")
	y: float = Field(description="Position or extension in millimeters")
	z_offset: float = Field(description="Position or extension in millimeters")
	slide_extension: float = Field(description="Position or extension in millimeters")
	warnings: Optional[list[str]] = None


class MoveRequest(BaseModel):
	side: StorageModuleSide
	shelf_idx: int
	cell_idx: int


class MovePositionRequest(BaseModel):
	side: StorageModuleSide
	x: float = Field(description="Position or extension in millimeters")
	y: float = Field(description="Position or extension in millimeters")
	z_offset: float = 0.0


class JogRequest(BaseModel):
	axis: Literal["x", "y", "z", "stage"]
	delta: float = Field(description="Signed displacement in millimeters")


class TestRequest(BaseModel):
	side: StorageModuleSide
	shelf_idx: int
	cell_idx: int





def _build_test_location(request: TestRequest) -> StorageModuleCellLocation:
	return StorageModuleCellLocation(
		side=request.side,
		shelf_idx=request.shelf_idx,
		cell_idx=request.cell_idx,
	)


UNTESTABLE_CELL_STATUSES = (CellStatus.DISABLED, CellStatus.MAINTENANCE)


async def _ensure_cell_is_testable(
	grid, location: StorageModuleCellLocation, *, allow_tray_home: bool = False
) -> Cell:
	try:
		cell = grid.get_cell(location)
	except (IndexError, ValueError) as exc:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=str(exc),
		) from exc

	if cell.status in UNTESTABLE_CELL_STATUSES:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"cell is {cell.status.value} and cannot be used for calibration tests",
		)

	if not allow_tray_home:
		# Grip tests exercise the tray in its home; single-item transfers would record a fake
		# item there.
		async with AsyncSessionLocal() as session:
			if await is_reserved_bin_location(
				(location.side.value, location.shelf_idx, location.cell_idx), session
			):
				raise HTTPException(
					status_code=status.HTTP_400_BAD_REQUEST,
					detail="cell is a bin home and cannot be used for this calibration test",
				)

	return cell


def _build_laser_auto_calibration_scope(
	side: StorageModuleSide | None,
	shelf_idx: int | None,
	cell_idx: int | None,
	mode: Literal["all", "both"] | None,
	include_angle_calibration: bool | None,
	include_extension_calibration: bool | None,
) -> LaserAutoCalibrationScope:
	if shelf_idx is not None and shelf_idx < 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="shelf_idx must be non-negative",
		)

	if cell_idx is not None and cell_idx < 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="cell_idx must be non-negative",
		)

	if mode == "both":
		if side is not None or shelf_idx is not None or cell_idx is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="side, shelf_idx, and cell_idx cannot be used with both mode",
			)

		return LaserAutoCalibrationScope(
			mode="both",
			include_angle_calibration=include_angle_calibration is not False,
			include_extension_calibration=include_extension_calibration is not False,
		)

	if side is None:
		if shelf_idx is not None or cell_idx is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="side is required when shelf_idx or cell_idx is provided",
			)

		return LaserAutoCalibrationScope(
			mode="all",
			include_angle_calibration=include_angle_calibration is not False,
			include_extension_calibration=include_extension_calibration is not False,
		)

	if cell_idx is not None and shelf_idx is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="shelf_idx is required when cell_idx is provided",
		)

	if shelf_idx is None:
		return LaserAutoCalibrationScope(
			mode="side",
			side=side,
			include_angle_calibration=include_angle_calibration is not False,
			include_extension_calibration=include_extension_calibration is not False,
		)

	if cell_idx is None:
		return LaserAutoCalibrationScope(
			mode="shelf",
			side=side,
			shelf_idx=shelf_idx,
			include_angle_calibration=False,
			include_extension_calibration=False,
		)

	return LaserAutoCalibrationScope(
		mode="cell",
		side=side,
		shelf_idx=shelf_idx,
		cell_idx=cell_idx,
		include_angle_calibration=False,
		include_extension_calibration=False,
	)


def _build_coordinate_for_location(grid, location: StorageModuleCellLocation) -> AxisCoordinate:
	calibration_point = grid.get_cell_coordinate(location)

	return AxisCoordinate(
		x=calibration_point.x,
		y=calibration_point.y,
		s=0 if location.side == StorageModuleSide.FRONT else 1,
		z_offset=calibration_point.z_offset,
	)


def _build_cycle_test_item(cell, location: StorageModuleCellLocation) -> CellItem:
	return CellItem(
		dimensions=Dimensions(
			width=max(float(cell.dimensions.width) * 0.8, 1.0),
			height=max(float(cell.dimensions.height) * 0.8, 1.0),
			length=max(float(cell.dimensions.length) * 0.8, 1.0),
		),
		data={
			"name": f"calibration test {location.side.value}:{location.shelf_idx}:{location.cell_idx}",
		},
	)


class SaveRequest(BaseModel):
	side: StorageModuleSide
	shelf_idx: int
	cell_idx: int
	x: float = Field(description="Position or extension in millimeters")
	y: float = Field(description="Position or extension in millimeters")
	z_offset: float = Field(description="Position or extension in millimeters")
	status: Optional[str] = None
	is_manual: Optional[bool] = None
	dimensions: Optional[Dimensions] = Field(
		None,
		description="Cell width, height, and length in millimeters; omit to keep the current dimensions."
	)


async def _shelf_stage_extension_mm(worker: Any, side: StorageModuleSide) -> float | None:
	"""The stage extension picks at this side run at, for stamping a hand-saved z.

	Not the live stage: a z typed on the cell calibration page is saved with the stage wherever
	it happens to be, often retracted, while every pick and test extends to this value.
	"""
	tool = getattr(getattr(worker, "_robot", None), "_tool", None)

	if tool is None:
		return None

	name = "STAGE_EXTENSION_SHELF_FRONT" if side == StorageModuleSide.FRONT else "STAGE_EXTENSION_SHELF_BACK"

	try:
		value = await tool.get_param(name)
	except Exception:
		logger.warning("calibration_save_stage_extension_failed", exc_info=True)
		return None

	return None if value is None else float(value)


class CalibrationStatusResponse(BaseModel):
	needs_home: bool
	needs_home_reason: Optional[str] = None
	needs_home_axes: list[str] = Field(default_factory=list)


class LaserReadingResponse(BaseModel):
	distance_mm: float


_current_position: Optional[PositionResponse] = None
_current_side: int = 0
_current_side_name: str = "front_module"


def _build_error_detail(
	message: str,
	*,
	code: str,
	needs_home: bool = False,
	needs_home_axes: Optional[list[str]] = None,
	reason: Optional[str] = None,
) -> dict[str, object]:
	detail: dict[str, object] = error_detail(message, code)
	if needs_home:
		detail["needs_home"] = True
		if needs_home_axes is not None:
			detail["needs_home_axes"] = needs_home_axes
	if reason:
		detail["reason"] = reason
	return detail


def _ensure_ready(worker) -> None:
	if worker.needs_home():
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_build_error_detail(
				"Home required before calibration movement.",
				code="home_required",
				needs_home=True,
				needs_home_axes=worker.needs_home_axes(),
				reason=worker.needs_home_reason(),
			),
		)


def _clamp_value(value: float, limit: float, label: str, warnings: list[str]) -> float:
	clamped = max(0.0, min(limit, value))
	if clamped != value:
		warnings.append(
			f"{label} limit reached; requested {value:.2f}mm, clamped to {clamped:.2f}mm (limit {limit:.2f}mm)"
		)
	return clamped


def _clamp_motor_value(value: float, motor, label: str, warnings: list[str]) -> float:
	lower_limit = float(getattr(motor._params, "lower_lim", 0.0))
	upper_limit = float(motor._params.lim)
	clamped = max(lower_limit, min(upper_limit, value))

	if clamped != value:
		warnings.append(
			f"{label} limit reached; requested {value:.2f}mm, clamped to {clamped:.2f}mm (range {lower_limit:.2f}-{upper_limit:.2f}mm)"
		)

	return clamped


async def _get_current_stage_extension(tool, fallback: float = 0.0) -> float:
	if getattr(tool, "_ms", None) is None:
		return fallback

	try:
		return await tool._ms.get_position()
	except Exception:
		return fallback


def _infer_axes_from_error(worker, exc: Exception) -> Optional[list[str]]:
	match = _MOTOR_ID_RE.search(str(exc))
	if not match:
		return None

	try:
		motor_id = int(match.group(1))
	except ValueError:
		return None

	tool = worker._robot._tool
	axis_lookup = {
		"x_axis": tool._mx,
		"y_axis": tool._my,
		"z_axis": tool._mg,
		"stage": tool._ms,
		"rotate": tool._mr,
	}
	for axis, motor in axis_lookup.items():
		if motor is not None and getattr(motor, "_motor_id", None) == motor_id:
			return [axis]

	return None


def _reason_matches_axis_or_motor(reason: Optional[str], axis: str, motor) -> bool:
	if not reason:
		return False

	reason_lower = reason.lower()
	if axis.lower() in reason_lower:
		return True

	match = _MOTOR_ID_RE.search(reason)
	if not match:
		return False

	try:
		motor_id = int(match.group(1))
	except ValueError:
		return False

	return motor is not None and getattr(motor, "_motor_id", None) == motor_id


def _clear_matching_tool_motor_crash_state(worker, axes: list[str]) -> None:
	if getattr(worker, "_is_crashed", False) is False:
		return

	reason = getattr(worker, "_crash_reason", None)
	tool = worker._robot._tool
	axis_to_motors = {
		"stage": tool._ms,
		"z_axis": tool._mg,
	}

	for axis in axes:
		if _reason_matches_axis_or_motor(reason, axis, axis_to_motors.get(axis)) is True:
			worker.clear_crash_state()
			return


async def _clear_motor_alarm(motor) -> None:
	if motor is not None:
		await motor.clear_alarm()


async def _clear_motors_for_move(
	tool,
	*,
	gantry: bool = False,
	gripper: bool = False,
	stage: bool = False,
	rotation: bool = False,
) -> None:
	if gantry:
		await _clear_motor_alarm(tool._mx)
		await _clear_motor_alarm(tool._my)
	if gripper:
		await _clear_motor_alarm(tool._mg)
	if stage:
		await _clear_motor_alarm(tool._ms)
	if rotation:
		await _clear_motor_alarm(tool._mr)


async def _retract_tool_for_move(
	tool,
	worker,
	*,
	blocked_message: Optional[str] = None,
) -> None:
	"""Bring Z and stage back to their retract positions before a manual move.

	Rotation only clears the gantry while both tool axes are retracted (the rotation
	clearance guard rejects anything past ROTATION_Z_MAX_EXTENSION_MM /
	ROTATION_STAGE_MAX_EXTENSION_MM). Routes that rotate have to retract explicitly first:
	`tool.move()` does retract, but only as part of a gantry move, so it runs after a
	rotation that already blocked, and its fixed 5 mm threshold is looser than the rotation
	guard for a machine configured with a smaller `ROTATION_Z_MAX_EXTENSION_MM`.

	Retracting with an item gripped is the same thing `test_gripper` already does, so this
	helps rather than hurts an aborted pick/place; an aborted IO transfer needs homing
	before it continues either way.

	A motor fault during the retract means the axes lost their position, so it goes through
	`_raise_motor_error` like every other move in this module: 409 plus `needs_home`.

	`blocked_message` reports a retract that finished with the tool still past the rotation
	clearance limits as blocked (409, no `needs_home`) instead of as a motor fault, the same
	way the rotation route already reports clearance failures.
	"""
	try:
		await tool.retract()
	except MovementError as e:
		if blocked_message is None:
			raise
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_build_error_detail(f"{blocked_message}: {str(e)}", code="rotation_blocked"),
		) from e
	except (MotorError, HardwareMotorError, ToolError) as e:
		await _raise_motor_error(worker, f"Failed to retract: {str(e)}", e)


async def _raise_motor_error(
	worker,
	message: str,
	exc: Exception,
	*,
	axes: Optional[list[str]] = None,
) -> None:
	if axes is None:
		axes = _infer_axes_from_error(worker, exc)
	worker.set_needs_home(message, axes=axes)
	await worker.get_status_snapshot(force_refresh=True)
	raise HTTPException(
		status_code=status.HTTP_409_CONFLICT,
		detail=_build_error_detail(
			message,
			code="motor_fault",
			needs_home=True,
			needs_home_axes=worker.needs_home_axes(),
		),
	) from exc


def _side_to_s(side: StorageModuleSide) -> int:
	return side_to_s(side)


def _get_shelf(grid, side: StorageModuleSide, shelf_idx: int):
	try:
		return grid.get_shelf(side, shelf_idx)
	except IndexError as e:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid shelf_idx: {shelf_idx}",
		) from e


def _get_cell(shelf, cell_idx: int):
	try:
		return shelf.get_cell(cell_idx)
	except IndexError as e:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid cell_idx: {cell_idx}",
		) from e


def _validate_non_negative_index(index: int, label: str) -> None:
	if index < 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid {label}: {index}",
		)


def _find_shelf(grid, side: StorageModuleSide, shelf_idx: int):
	for shelf in grid.get_side(side):
		if shelf.shelf_idx == shelf_idx:
			return shelf

	return None


def _find_cell(shelf, cell_idx: int):
	for cell in shelf.cells:
		if cell.cell_idx == cell_idx:
			return cell

	return None


def _build_default_shelf_template() -> Shelf:
	return Shelf(
		shelf_idx=0,
		cells=[],
		divider_width=_DEFAULT_DIVIDER_WIDTH,
	)


def _build_default_cell_template() -> Cell:
	return Cell(
		cell_idx=0,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=None,
		dimensions=Dimensions(
			width=_DEFAULT_CELL_WIDTH,
			height=_DEFAULT_CELL_HEIGHT,
			length=_DEFAULT_CELL_LENGTH,
		),
	)

def _get_shelf_template(grid, side: StorageModuleSide) -> Shelf:
	shelves = grid.get_side(side)

	if len(shelves) > 0:
		return shelves[-1]

	other_side = StorageModuleSide.BACK if side == StorageModuleSide.FRONT else StorageModuleSide.FRONT
	other_shelves = grid.get_side(other_side)

	if len(other_shelves) > 0:
		return other_shelves[-1]

	return _build_default_shelf_template()


def _get_cell_template(grid, side: StorageModuleSide, shelf) -> Cell:
	if len(shelf.cells) > 0:
		return shelf.cells[-1]

	for candidate_shelf in reversed(grid.get_side(side)):
		if len(candidate_shelf.cells) > 0:
			return candidate_shelf.cells[-1]

	other_side = StorageModuleSide.BACK if side == StorageModuleSide.FRONT else StorageModuleSide.FRONT

	for candidate_shelf in reversed(grid.get_side(other_side)):
		if len(candidate_shelf.cells) > 0:
			return candidate_shelf.cells[-1]

	return _build_default_cell_template()


def _ensure_shelf(grid, side: StorageModuleSide, shelf_idx: int):
	shelf = _find_shelf(grid, side, shelf_idx)

	if shelf is not None:
		return shelf

	template = _get_shelf_template(grid, side)
	new_shelf = Shelf(
		shelf_idx=shelf_idx,
		cells=[],
		divider_width=template.divider_width,
	)
	grid.get_side(side).append(new_shelf)

	return new_shelf


def _ensure_cell(grid, side: StorageModuleSide, shelf, cell_idx: int):
	cell = _find_cell(shelf, cell_idx)

	if cell is not None:
		return cell

	template = _get_cell_template(grid, side, shelf)
	new_cell = Cell(
		cell_idx=cell_idx,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=None,
		dimensions=template.dimensions.model_copy(deep=True),
	)
	shelf.cells.append(new_cell)

	return new_cell


async def _move_tool_to_coordinate(
	worker,
	coord: AxisCoordinate,
	*,
	side_name: StorageModuleSide,
	warnings: Optional[list[str]] = None,
) -> PositionResponse:
	global _current_position, _current_side, _current_side_name

	tool = worker._robot._tool

	try:
		async with manual_motion_guard("calibration_move", worker=worker):
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			await tool.move(coord)

			target_position: Literal["front", "back"] = "front" if coord.s == 0 else "back"
			await tool.rotate_to_position(target_position)

			if tool._mg is not None:
				grip_ext = await tool.get_param("Z_EXTENSION_PLACE")
				if grip_ext is not None:
					grip_rpm = await tool.get_param("Z_MOVE_RPM")
					grip_accel = await tool.get_param("Z_MOVE_ACCEL")
					target_z = grip_ext + coord.z_offset
					await tool._mg.move_to_position(target_z, rpm=grip_rpm, accel=grip_accel)
	except (MotorError, HardwareMotorError, ToolError) as e:
		await _raise_motor_error(worker, f"Failed to move to cell: {str(e)}", e)

	slide_extension = await _get_current_stage_extension(tool)

	_current_side = coord.s
	_current_side_name = side_name.value
	warnings_payload = warnings if warnings else None
	_current_position = PositionResponse(
		x=coord.x,
		y=coord.y,
		z_offset=coord.z_offset,
		slide_extension=slide_extension,
		warnings=warnings_payload,
	)

	return _current_position


async def _build_clamped_manual_coordinate(tool, request: MovePositionRequest) -> tuple[AxisCoordinate, list[str]]:
	warnings: list[str] = []
	x = request.x
	y = request.y
	z_offset = request.z_offset

	if tool._mx is not None:
		x = _clamp_motor_value(x, tool._mx, "X axis", warnings)

	if tool._my is not None:
		y = _clamp_motor_value(y, tool._my, "Y axis", warnings)

	if tool._mg is not None:
		grip_ext = await tool.get_param("Z_EXTENSION_PLACE")

		if grip_ext is not None:
			target_z = _clamp_motor_value(grip_ext + z_offset, tool._mg, "Z axis", warnings)
			z_offset = target_z - grip_ext

	return (
		AxisCoordinate(
			x=x,
			y=y,
			s=_side_to_s(request.side),
			z_offset=z_offset,
		),
		warnings,
	)


def _get_stage_limit(tool) -> float:
	if tool._ms is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Stage motor not configured",
		)
	return tool._ms._params.lim


def _get_stage_limit_from_config(config) -> Optional[float]:
	try:
		stage_motor = config.robot.tool.motors.by_name("stage")
	except KeyError:
		return None
	return stage_motor.lim


def _validate_stage_target(value: float, limit: float, label: str) -> None:
	if value < 0 or value > limit:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"{label} must be between 0 and {limit} (got {value})",
		)


@router.get(
    "/status",
    response_model=CalibrationStatusResponse,
    summary="Read calibration readiness",
    responses=response_docs(errors=(401, 409, 503)),
)
async def get_calibration_status() -> CalibrationStatusResponse:
	"""Read calibration readiness.

	Reports whether homing is required and which axes need it. Does not home or move the
	robot. Use this before a calibration procedure.
	"""
	worker = await get_robot_worker_or_503()

	return CalibrationStatusResponse(
		needs_home=worker.needs_home(),
		needs_home_reason=worker.needs_home_reason(),
		needs_home_axes=worker.needs_home_axes(),
	)


@router.get(
    "/storage",
    summary="Read the calibration grid",
    responses=response_docs(model=StorageGrid, errors=(401, 409, 503)),
)
async def get_storage():
	"""Read the calibration grid.

	Returns the storage grid used by calibration, including per-cell calibration points.
	This is geometry, not a command result or a live inventory snapshot.
	"""
	worker = await get_robot_worker_or_503()

	grid = worker._robot._grid
	return grid.model_dump()


@router.get(
    "/laser/read",
    response_model=LaserReadingResponse,
    summary="Read laser distance",
    responses=response_docs(errors=(401, 409, 503)),
)
async def read_laser() -> LaserReadingResponse:
	"""Read laser distance.

	Returns the current laser distance in millimeters. Requires an initialized robot; a
	failed read returns `503`. Does not start an automatic calibration session.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	try:
		distance_mm = await worker._robot.get_laser_distance()
	except Exception as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=f"Failed to read laser distance: {str(exc)}",
		) from exc

	return LaserReadingResponse(distance_mm=float(distance_mm))


@router.get(
    "/laser/status",
    response_model=LaserAutoCalibrationSnapshot,
    summary="Read automatic calibration progress",
    responses=response_docs(errors=(401, 409, 503)),
)
async def get_laser_auto_calibration_status() -> LaserAutoCalibrationSnapshot:
	"""Read automatic calibration progress.

	Returns the current automatic laser-calibration snapshot, including scope and progress.
	Does not start or abort a session.
	"""
	manager = get_laser_auto_calibration_manager()
	return await manager.get_snapshot()


@router.post(
    "/laser/abort",
    dependencies=[ROBOT_CONTROL],
    response_model=LaserAutoCalibrationSnapshot,
    summary="Abort automatic laser calibration",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def abort_laser_auto_calibration() -> LaserAutoCalibrationSnapshot:
	"""Abort automatic laser calibration.

	Requests cancellation of the active automatic calibration session and returns its
	snapshot. A state conflict returns `409`; inspect the snapshot before starting another
	operation.
	"""
	manager = get_laser_auto_calibration_manager()

	try:
		return await manager.abort()
	except RuntimeError as exc:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=str(exc),
		) from exc


class LaserZStallSetting(BaseModel):
	enabled: bool


@router.get(
    "/laser/z-stall",
    response_model=LaserZStallSetting,
    summary="Read the grip edge z setting",
    responses=response_docs(errors=(401,)),
)
async def get_laser_z_stall_setting() -> LaserZStallSetting:
	"""Read the grip edge z setting.

	Returns whether automatic calibration sets each cell's z from where the gripper starts to
	grip it. The setting belongs to the machine (`robot.grid.scan.z_stall_anchor_enabled`, named
	for the contact probe it replaced), not to a run.
	"""
	return LaserZStallSetting(enabled=get_root_config().robot.grid.scan.z_stall_anchor_enabled is True)


@router.post(
    "/laser/z-stall",
    dependencies=[ROBOT_CONTROL],
    response_model=LaserZStallSetting,
    summary="Save the grip edge z setting",
    responses=response_docs(errors=(401, 403, 409)),
)
async def save_laser_z_stall_setting(request: LaserZStallSetting) -> LaserZStallSetting:
	"""Save the grip edge z setting.

	Persists `robot.grid.scan.z_stall_anchor_enabled`. When enabled, every later automatic
	calibration that holds the bottom-left reference cell steps each cell's z until the gripper
	grips it and sets the cell's z just past that edge. Returns `409` while automatic calibration runs. Does not move.
	"""
	await ensure_laser_auto_calibration_not_running()

	config = get_root_config().model_copy(deep=True)
	config.robot.grid.scan.z_stall_anchor_enabled = request.enabled
	save_root_config(config)

	return LaserZStallSetting(enabled=request.enabled)


TERMINAL_LASER_AUTO_CALIBRATION_STATES = {"completed", "failed", "cancelled"}


@router.get(
    "/laser/stream",
    dependencies=[ROBOT_CONTROL],
    summary="Start or observe automatic laser calibration",
    responses=response_docs(
        errors=(400, 401, 403, 409, 503),
        existing=stream_docs(
            "text/event-stream",
            "Automatic calibration snapshots encoded as JSON in SSE data frames. GET may start motion unless subscribe_only=true.",
            None,
        ),
    ),
    response_class=Response,
)
async def stream_laser_auto_calibration(
	request: Request,
	side: StorageModuleSide | None = None,
	shelf_idx: int | None = None,
	cell_idx: int | None = None,
	mode: Literal["all", "both"] | None = None,
	include_angle_calibration: bool | None = None,
	include_extension_calibration: bool | None = None,
	hold_y_enabled: bool | None = None,
	subscribe_only: bool = False,
) -> EventSourceResponse:
	"""Start or observe automatic laser calibration.

	**This GET can start physical motion.** With the default `subscribe_only=false`, starts
	or joins an automatic calibration session for the supplied scope. Set
	`subscribe_only=true` to observe without starting one. Conflicting scopes or a busy
	robot return `409`; invalid scope returns `400`.

	Returns Server-Sent Events with JSON calibration snapshots. A client disconnect
	unsubscribes that client; use the abort endpoint to stop the session. Do not infer
	cancellation from a closed connection. Optional scope parameters select cells,
	both sides, or the angle/extension calibration phases. `hold_y_enabled=false` parks
	Y on its brake between moves instead of holding it energised for the session. Every run
	frames its offsets from the hand-calibrated bottom-left cell of the sides it covers and
	returns `400` before moving when one of those cells is missing or not marked manual, or
	when a side or module run has no z source (z stall probe, a reference cell whose z was
	saved with its stage extension, or a z model pinned to the live Z_EXTENSION_PLACE).
	"""
	requested_scope = _build_laser_auto_calibration_scope(
		side,
		shelf_idx,
		cell_idx,
		mode,
		include_angle_calibration,
		include_extension_calibration,
	).model_copy(
		update={
			"hold_y_enabled": hold_y_enabled is not False,
		}
	)
	manager = get_laser_auto_calibration_manager()
	is_active = await manager.is_active()

	if is_active is False and subscribe_only is False:
		try:
			worker = await get_robot_worker()
		except ValueError as exc:
			raise HTTPException(
				status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
				detail="Robot is not initialized",
			) from exc

		_ensure_ready(worker)

		if getattr(worker, "_current_command", None) is not None:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="The robot is busy with another task.",
			)

		if getattr(worker, "_demo_cycling", False) is True:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="The robot is running the demo cycle.",
			)

		try:
			# Reserve laser motion under the same gate as bin workflow creation.
			# The manager records its task synchronously before this gate is released.
			await manager.ensure_session_started(worker, requested_scope)
		except RuntimeError as exc:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail=str(exc),
			) from exc
		except ValueError as exc:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail=str(exc),
			) from exc
	else:
		snapshot = await manager.get_snapshot()

		if snapshot.requested_scope.model_dump() != requested_scope.model_dump():
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="Laser auto calibration is already running with a different scope",
			)

	queue, initial_snapshot = await manager.subscribe()

	async def event_generator():
		try:
			yield {
				"event": "snapshot",
				"data": initial_snapshot.model_dump_json(),
			}
			if initial_snapshot.state in TERMINAL_LASER_AUTO_CALIBRATION_STATES:
				return

			while True:
				if await request.is_disconnected() is True:
					break

				try:
					snapshot = await asyncio.wait_for(queue.get(), timeout=1.0)
				except asyncio.TimeoutError:
					continue

				yield {
					"event": "snapshot",
					"data": snapshot.model_dump_json(),
				}

				if snapshot.state in TERMINAL_LASER_AUTO_CALIBRATION_STATES:
					break
		finally:
			await manager.unsubscribe(queue)

	return EventSourceResponse(
		event_generator(),
		ping=15,
		send_timeout=30,
		headers={"Cache-Control": "no-cache"},
	)


@router.post(
    "/move",
    dependencies=[ROBOT_CONTROL],
    response_model=PositionResponse,
    summary="Move to a cell calibration point",
    responses=response_docs(errors=(400, 401, 403, 409, 503)),
)
@serialize_calibration_state
async def move_to_cell(request: MoveRequest) -> PositionResponse:
	"""Move to a cell calibration point.

	Moves the tool to the selected side and zero-based shelf/cell calibration point. Returns
	the current calibration position. A cell without a point returns `400`. Starts the
	position context used by subsequent jog operations.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	grid = worker._robot._grid

	side_enum = request.side
	shelf = _get_shelf(grid, side_enum, request.shelf_idx)
	cell = _get_cell(shelf, request.cell_idx)

	if cell.calibration_point is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Cell has no calibration point",
		)

	cal_point = cell.calibration_point

	coord = AxisCoordinate(
		x=cal_point.x,
		y=cal_point.y,
		s=_side_to_s(request.side),
		z_offset=cal_point.z_offset,
	)

	return await _move_tool_to_coordinate(worker, coord, side_name=request.side)


@router.post(
    "/move/position",
    dependencies=[ROBOT_CONTROL],
    response_model=PositionResponse,
    summary="Move to a calibration coordinate",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
@serialize_calibration_state
async def move_to_position(request: MovePositionRequest) -> PositionResponse:
	"""Move to a calibration coordinate.

	Moves the tool to the supplied coordinate in millimeters and establishes the calibration
	position context. The response may include warnings for adjusted coordinates. Requires
	available hardware and exclusive manual-motion access.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)
	tool = worker._robot._tool

	coord, warnings = await _build_clamped_manual_coordinate(tool, request)

	return await _move_tool_to_coordinate(worker, coord, side_name=request.side, warnings=warnings)


@router.post(
    "/jog",
    dependencies=[ROBOT_CONTROL],
    response_model=PositionResponse,
    summary="Jog the active calibration position",
    responses=response_docs(errors=(400, 401, 403, 409, 500, 503)),
)
@serialize_calibration_state
async def jog_axis(request: JogRequest) -> PositionResponse:
	"""Jog the active calibration position.

	Moves the selected axis by signed `delta` millimeters and updates the in-memory
	calibration position. Move to a cell or position first; otherwise returns `400`. Changes
	are not persisted until saved.
	"""
	global _current_position

	await ensure_laser_auto_calibration_not_running()

	if _current_position is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="No active calibration session. Move to a cell first.",
		)

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool

	x_limit = tool._mx._params.lim if tool._mx is not None else 1000
	y_limit = tool._my._params.lim if tool._my is not None else 1500
	stage_limit = tool._ms._params.lim if tool._ms is not None else 75

	new_x = _current_position.x
	new_y = _current_position.y
	new_z = _current_position.z_offset
	new_slide = _current_position.slide_extension
	warnings: list[str] = []

	try:
		async with manual_motion_guard("calibration_jog", worker=worker):
			if request.axis == "x":
				if tool._mx is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="X motor not configured",
					)
				await _clear_motors_for_move(tool, gantry=True)
				new_x = _clamp_value(new_x + request.delta, x_limit, "X axis", warnings)
				await tool._mx.move_to_position(new_x)
			elif request.axis == "y":
				if tool._my is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="Y motor not configured",
					)
				await _clear_motors_for_move(tool, gantry=True)
				new_y = _clamp_value(new_y + request.delta, y_limit, "Y axis", warnings)
				await tool._my.move_to_position(new_y)
			elif request.axis == "z":
				if tool._mg is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="Gripper motor not configured",
					)
				await _clear_motors_for_move(tool, gripper=True)
				grip_ext = await tool.get_param("Z_EXTENSION_PLACE")
				if grip_ext is None:
					raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Z_EXTENSION_PLACE not configured")
				current_gripper_pos = grip_ext + _current_position.z_offset
				z_limit = tool._mg._params.lim
				new_gripper_pos = _clamp_value(current_gripper_pos + request.delta, z_limit, "Z axis", warnings)
				new_z = new_gripper_pos - grip_ext
				grip_rpm = await tool.get_param("Z_MOVE_RPM")
				grip_accel = await tool.get_param("Z_MOVE_ACCEL")
				await tool._mg.move_to_position(new_gripper_pos, rpm=grip_rpm, accel=grip_accel)
			elif request.axis == "stage":
				if tool._ms is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="Stage motor not configured",
					)
				await _clear_motors_for_move(tool, stage=True)
				new_slide = _clamp_value(new_slide + request.delta, stage_limit, "Stage", warnings)
				await tool._ms.move_to_position(new_slide)
	except (MotorError, HardwareMotorError) as e:
		axes: Optional[list[str]] = None
		if request.axis == "z":
			axes = ["z_axis"]
		elif request.axis == "stage":
			axes = ["stage"]
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e, axes=axes)

	warnings_payload = warnings if warnings else None
	_current_position = PositionResponse(
		x=new_x,
		y=new_y,
		z_offset=new_z,
		slide_extension=new_slide,
		warnings=warnings_payload,
	)

	return _current_position


@router.post(
    "/test",
    dependencies=[ROBOT_CONTROL],
    summary="Test a cell pick or place",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
@serialize_calibration_state
async def test_gripper(
	request: TestRequest,
	lift: Annotated[float | None, Query(gt=0, le=MAX_PICK_LIFT_MM)] = None,
):
	"""Test a cell pick or place.

	Runs the selected cell gripper test on physical hardware. Optional `lift` requests a
	bounded pick/lift/place test. Tray homes are supported; disabled or maintenance cells
	are rejected. This tests
	motion without saving new calibration coordinates. To queue many cells without
	depending on the browser tab staying active, use the cell test queue.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	await _perform_quick_test(worker, _build_test_location(request), lift)
	return {"success": True}


async def _perform_quick_test(
	worker,
	location: StorageModuleCellLocation,
	lift: float | None,
) -> None:
	"""Run one cell gripper test; shared by the single-cell endpoint and the test queue."""
	global _current_position, _current_side

	_ensure_ready(worker)

	grid = worker._robot._grid

	await _ensure_cell_is_testable(grid, location, allow_tray_home=True)

	try:
		coordinate = _build_coordinate_for_location(grid, location)
	except (IndexError, ValueError, Exception) as exc:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=str(exc),
		) from exc

	tool = worker._robot._tool

	async with manual_motion_guard("calibration_test_gripper", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			await tool.retract()
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Failed to retract: {str(e)}", e)

		coord = coordinate
		_current_position = PositionResponse(
			x=coord.x,
			y=coord.y,
			z_offset=coord.z_offset,
			slide_extension=await _get_current_stage_extension(tool),
		)
		_current_side = coord.s

		try:
			if lift is None:
				await tool.pick(coord)
				await tool.place(coord)
			else:
				await tool.pick_lift_place(coord, lift, QUICK_LIFT_HOLD_SECONDS)
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Test failed: {str(e)}", e)




GRIP_PROBE_MAX_POINTS = 24
GRIP_PROBE_MAX_DEVIATION_MM = 5.0


class GripProbeRequest(TestRequest):
	z_offsets: list[float] = Field(min_length=1, max_length=GRIP_PROBE_MAX_POINTS)


class GripProbePoint(BaseModel):
	z_offset: float
	gripped: bool
	object: str
	position: int
	current: int
	state: str


@router.post(
    "/grip-probe",
    dependencies=[ROBOT_CONTROL],
    response_model=list[GripProbePoint],
    summary="Read the gripper's object detection across z offsets at a cell",
    responses=response_docs(errors=(400, 401, 403, 409, 503)),
)
@serialize_calibration_state
async def grip_probe(request: GripProbeRequest) -> list[GripProbePoint]:
	"""Read the gripper's object detection across z offsets at a cell.

	Engineering diagnostic for finding a cell's working z window. At each z the tool extends
	empty as a pick does, splays the fingers and records what the gripper reports, then closes
	the fingers before retracting, so a tray is never withdrawn or dragged. Each z must lie
	within 5mm of the cell's stored z. Moves hardware; does not save calibration.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	location = _build_test_location(request)
	grid = worker._robot._grid

	await _ensure_cell_is_testable(grid, location, allow_tray_home=True)

	try:
		coordinate = _build_coordinate_for_location(grid, location)
	except (IndexError, ValueError, Exception) as exc:
		raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

	far = [z for z in request.z_offsets if abs(float(z) - float(coordinate.z_offset)) > GRIP_PROBE_MAX_DEVIATION_MM]

	if far:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"z offsets {far} are more than {GRIP_PROBE_MAX_DEVIATION_MM:.0f}mm from the cell's {coordinate.z_offset:.2f}",
		)

	tool = worker._robot._tool
	target_position: Literal["front", "back"] = "front" if coordinate.s == 0 else "back"
	points: list[GripProbePoint] = []

	async with manual_motion_guard("calibration_grip_probe", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			await tool.retract()
			await tool._gs.safe_open()
			await tool.move(coordinate)
			await tool.rotate_to_position(target_position)

			for z_offset in request.z_offsets:
				state = await probe_grip_at(tool, coordinate, z_offset)
				points.append(
					GripProbePoint(
						z_offset=float(z_offset),
						gripped=bool(state.gripped),
						object=state.obj.name,
						position=int(state.position),
						current=int(state.current),
						state=str(state),
					)
				)
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Grip probe failed after {len(points)} points: {str(e)}", e)

	return points


@router.post(
    "/test/cycle",
    dependencies=[ROBOT_CONTROL],
    summary="Test an IO transfer cycle",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 500, 503)),
)
async def test_cycle(request: TestRequest):
	"""Test an IO transfer cycle.

	Runs the IO pick/place cycle for the selected calibration cell. Tray homes are supported;
	disabled or maintenance cells are rejected. This moves the tool through the IO box; use
	only with the physical setup prepared for the test. Does not save calibration. To queue
	many cells without depending on the browser tab staying active, use the cell test queue.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	await _perform_cycle_test(worker, _build_test_location(request))
	return {"success": True}


async def _perform_cycle_test(worker, location: StorageModuleCellLocation) -> None:
	"""Run one IO transfer cycle test; shared by the single-cell endpoint and the test queue."""
	_ensure_ready(worker)

	robot = worker._robot
	tool = robot._tool
	grid = robot._grid

	cell = await _ensure_cell_is_testable(grid, location)

	try:
		coordinate = _build_coordinate_for_location(grid, location)
	except (IndexError, ValueError, Exception) as exc:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=str(exc),
		) from exc

	initial_item = cell.item.model_copy(deep=True) if cell.item is not None else None
	is_initially_occupied = cell.status == CellStatus.OCCUPIED
	cycle_item = initial_item

	if is_initially_occupied is False:
		cycle_item = _build_cycle_test_item(cell, location)

	async with manual_motion_guard("calibration_test_cycle", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			# An aborted cycle test can leave the tool extended; io_place/io_pick rotate before
			# they retract, so the rotation clearance guard would reject the whole cycle. A tray
			# left gripped from an aborted run comes in with the retract, which is what every
			# other recovery path in this file already does.
			await _retract_tool_for_move(tool, worker)

			await robot.io_place(coordinate)
			grid.clear_item(location)

			await robot.io_pick(coordinate)
			grid.store_item(location, cycle_item)

			grid.save()
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Cycle test failed: {str(e)}", e)
		except Exception as exc:
			raise HTTPException(
				status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
				detail=f"Cycle test failed: {exc}",
			) from exc


MAX_TEST_QUEUE_CELLS = 256


class TestQueueCell(BaseModel):
	shelf_idx: int = Field(ge=0)
	cell_idx: int = Field(ge=0)


class TestQueueRequest(BaseModel):
	side: StorageModuleSide
	mode: Literal["quick", "cycle"]
	cells: list[TestQueueCell] = Field(min_length=1, max_length=MAX_TEST_QUEUE_CELLS)
	lift: Optional[float] = Field(
		None,
		gt=0,
		le=MAX_PICK_LIFT_MM,
		description="Optional pick/lift/place height in millimeters for quick mode",
	)


class TestQueueSnapshot(BaseModel):
	state: Literal["idle", "running", "completed", "failed", "cancelled"] = "idle"
	job_id: Optional[str] = None
	side: Optional[str] = None
	mode: Optional[Literal["quick", "cycle"]] = None
	total: int = 0
	completed: int = 0
	current: Optional[TestQueueCell] = None
	failed: Optional[TestQueueCell] = None
	error: Optional[str] = None
	started_at: Optional[str] = None
	updated_at: Optional[str] = None


# The per-cell walk lives here instead of the browser so a frozen or throttled
# background tab cannot stall the queue mid-walk. One job at a time; the motion
# guard and cell checks inside each test still apply per cell.
_test_queue_lock = asyncio.Lock()
_test_queue_task: Optional[asyncio.Task[None]] = None
_test_queue_snapshot = TestQueueSnapshot()
_test_queue_abort_requested = False


def _test_queue_now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


async def _publish_test_queue_event(
	worker,
	snapshot: TestQueueSnapshot,
	cell: Optional[TestQueueCell] = None,
	ok: Optional[bool] = None,
) -> None:
	payload: dict[str, Any] = {
		"kind": "calibration_test_queue",
		"payload": snapshot.model_dump(),
	}
	if cell is not None:
		payload["payload"]["cell"] = cell.model_dump()
		payload["payload"]["ok"] = ok
	try:
		await worker.publish_event(payload)
	except Exception:
		# A failed publish must not stall the walk; the snapshot endpoint still
		# carries the state and the stream replays from the durable log.
		logger.warning("Failed to publish calibration_test_queue event", exc_info=True)


async def _finish_test_queue(
	worker,
	snapshot: TestQueueSnapshot,
	state: Literal["completed", "failed", "cancelled"],
	failed: Optional[TestQueueCell] = None,
	error: Optional[str] = None,
) -> None:
	snapshot.state = state
	snapshot.current = None
	snapshot.failed = failed
	snapshot.error = error
	snapshot.updated_at = _test_queue_now_iso()
	await _publish_test_queue_event(worker, snapshot)


def _test_queue_error_message(detail: Any) -> str:
	# Mirrors dev_ui formatErrorMessage so queue toasts match the old single-cell errors.
	if not isinstance(detail, dict):
		return str(detail)
	message = detail.get("message") or str(detail)
	reason = detail.get("reason")
	if reason and reason != message:
		return f"{message} ({reason})"
	if detail.get("needs_home") and reason is None:
		return f"{message} (home required)"
	return message


async def _run_cell_test_queue(
	worker,
	snapshot: TestQueueSnapshot,
	locations: list[StorageModuleCellLocation],
	lift: Optional[float],
) -> None:
	global _test_queue_abort_requested

	for location in locations:
		if _test_queue_abort_requested:
			await _finish_test_queue(worker, snapshot, "cancelled")
			return

		cell = TestQueueCell(shelf_idx=location.shelf_idx, cell_idx=location.cell_idx)
		snapshot.current = cell

		error: Optional[str] = None
		try:
			if snapshot.mode == "quick":
				async with _calibration_state_lock:
					await ensure_laser_auto_calibration_not_running()
					await _perform_quick_test(worker, location, lift)
			else:
				await ensure_laser_auto_calibration_not_running()
				await _perform_cycle_test(worker, location)
		except HTTPException as exc:
			error = _test_queue_error_message(exc.detail)
		except Exception as exc:
			logger.error("calibration test queue cell failed", exc_info=True)
			_, error = internal_error_detail(exc)

		snapshot.completed += 1
		snapshot.current = None
		snapshot.updated_at = _test_queue_now_iso()

		if error is not None:
			await _finish_test_queue(worker, snapshot, "failed", failed=cell, error=error)
			return

		await _publish_test_queue_event(worker, snapshot, cell, True)

	await _finish_test_queue(worker, snapshot, "completed")


@router.post(
    "/test/queue",
    dependencies=[ROBOT_CONTROL],
    response_model=TestQueueSnapshot,
    summary="Start a server-side calibration test queue",
    responses=response_docs(errors=(400, 401, 403, 409, 503)),
)
async def start_cell_test_queue(request: TestQueueRequest) -> TestQueueSnapshot:
	"""Start a server-side calibration test queue.

	Walks the given cells one at a time in the given order on the server, running the quick
	or cycle test for each, and stops at the first failure. Progress and the terminal state
	are published as `calibration_test_queue` events on `/api/robot/events`; the current
	state is also readable at `GET /api/calibration/test/queue`. Requires `robot:control`;
	returns `409` while another test queue is running.
	"""
	global _test_queue_task, _test_queue_snapshot, _test_queue_abort_requested

	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	async with _test_queue_lock:
		if _test_queue_task is not None and _test_queue_task.done() is False:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="a calibration test queue is already running",
			)

		_test_queue_abort_requested = False
		now = _test_queue_now_iso()
		snapshot = TestQueueSnapshot(
			state="running",
			job_id=uuid4().hex,
			side=request.side.value,
			mode=request.mode,
			total=len(request.cells),
			started_at=now,
			updated_at=now,
		)
		_test_queue_snapshot = snapshot
		locations = [
			StorageModuleCellLocation(
				side=request.side,
				shelf_idx=cell.shelf_idx,
				cell_idx=cell.cell_idx,
			)
			for cell in request.cells
		]
		_test_queue_task = asyncio.create_task(
			_run_cell_test_queue(worker, snapshot, locations, request.lift)
		)

	return snapshot.model_copy(deep=True)


@router.get(
    "/test/queue",
    response_model=TestQueueSnapshot,
    summary="Read the calibration test queue state",
    responses=response_docs(model=TestQueueSnapshot, errors=(401, 403)),
)
async def get_cell_test_queue() -> TestQueueSnapshot:
	"""Read the calibration test queue state.

	Returns the current or most recent queue snapshot; `state` is `idle` until the first
	queue starts on this process. Clients resuming a backgrounded or reloaded page use this
	to catch up between `calibration_test_queue` events.
	"""
	return _test_queue_snapshot.model_copy(deep=True)


@router.post(
    "/test/queue/abort",
    dependencies=[ROBOT_CONTROL],
    response_model=TestQueueSnapshot,
    summary="Abort the calibration test queue",
    responses=response_docs(model=TestQueueSnapshot, errors=(401, 403, 409)),
)
async def abort_cell_test_queue() -> TestQueueSnapshot:
	"""Abort the calibration test queue.

	Requests cancellation before the next cell; the cell currently moving finishes first,
	matching the single-cell tests. The queue publishes a `cancelled` terminal event once
	it stops. Requires `robot:control`; returns `409` when no queue is running.
	"""
	global _test_queue_abort_requested

	if _test_queue_task is None or _test_queue_task.done() is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="no calibration test queue is running",
		)

	_test_queue_abort_requested = True
	return _test_queue_snapshot.model_copy(deep=True)


def _validate_saved_dimensions(dimensions: Dimensions) -> None:
	for field, value in (
		("width", dimensions.width),
		("height", dimensions.height),
		("length", dimensions.length),
	):
		if not value > 0:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail=f"Dimension {field} must be greater than zero",
			)


@router.post(
    "/save",
    dependencies=[ROBOT_CONTROL],
    summary="Save a cell calibration point",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
async def save_calibration(request: SaveRequest):
	"""Save a cell calibration point.

	Persists coordinates, optional status, manual marker, and dimensions for the selected
	cell. Tray homes allow coordinate, manual-marker, status, and dimension updates.
	Invalid status returns `400`.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	grid = worker._robot._grid
	_validate_non_negative_index(request.shelf_idx, "shelf_idx")
	_validate_non_negative_index(request.cell_idx, "cell_idx")

	new_status = None
	if request.status is not None:
		try:
			new_status = CellStatus(request.status)
		except ValueError:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail=f"Invalid status: {request.status}. Must be one of: available, occupied, maintenance, disabled",
			)

	# Read tray homes while the caller's request session is open, so a coordinate save
	# cannot silently change the cell a checked-out tray returns to.
	async with AsyncSessionLocal() as session:
		reserved_homes = await reserved_bin_locations(session)
	if (request.side.value, request.shelf_idx, request.cell_idx) in reserved_homes:
		try:
			grid.get_cell(StorageModuleCellLocation(
				side=request.side, shelf_idx=request.shelf_idx, cell_idx=request.cell_idx
			))
		except (IndexError, ValueError) as exc:
			raise HTTPException(status_code=409, detail="Bin home cell is missing from storage") from exc

	side_enum = request.side
	shelf = _ensure_shelf(grid, side_enum, request.shelf_idx)

	if request.dimensions is not None:
		_validate_saved_dimensions(request.dimensions)

	cell = _ensure_cell(grid, side_enum, shelf, request.cell_idx)
	previous_point = cell.calibration_point

	# The z offset only means anything at the stage extension it is used at, so record that with
	# the coordinate. Keep the existing stamp when a save does not touch z, since a z nobody
	# re-measured belongs to the stage extension of its own save.
	captured_stage_mm = None if previous_point is None else previous_point.z_captured_at_stage_mm

	if previous_point is None or float(previous_point.z_offset) != float(request.z_offset):
		captured_stage_mm = await _shelf_stage_extension_mm(worker, request.side)

	cell.calibration_point = CalibrationCoordinate(
		x=request.x,
		y=request.y,
		s=_side_to_s(request.side),
		z_offset=request.z_offset,
		z_captured_at_stage_mm=captured_stage_mm,
	)

	if request.is_manual is not None:
		cell.is_manual = request.is_manual

	if request.dimensions is not None:
		cell.dimensions = request.dimensions.model_copy(deep=True)

	if new_status is not None:
		cell.status = new_status

	grid.save()

	return {"success": True}


@router.post(
    "/home_tool_motors",
    dependencies=[ROBOT_CONTROL],
    response_model=CalibrationStatusResponse,
    summary="Home the tool stage and gripper",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def home_tool_motors() -> CalibrationStatusResponse:
	"""Home the tool stage and gripper.

	Homes the stage and gripper motors, refreshes worker status, and returns remaining
	homing requirements. This moves hardware and is unavailable during automatic laser
	calibration.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	tool = worker._robot._tool

	if tool._ms is None or tool._mg is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Tool motors not configured",
		)

	try:
		async with manual_motion_guard("calibration_home_tool_motors", worker=worker, allow_tray_on_tool=True):
			await tool._ms.home()
			await tool._mg.home()
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Failed to home tool motors: {str(e)}", e, axes=["stage", "z_axis"])

	worker.clear_needs_home_axes(["stage", "z_axis"])
	_clear_matching_tool_motor_crash_state(worker, ["stage", "z_axis"])
	await worker.get_status_snapshot(force_refresh=True)

	return CalibrationStatusResponse(
		needs_home=worker.needs_home(),
		needs_home_reason=worker.needs_home_reason(),
		needs_home_axes=worker.needs_home_axes(),
	)


IOBoxPositionName = Literal["low", "high", "multi_item"]
IOBoxAxis = Literal["x", "y", "z", "stage"]

# Pickup low/high share one tool extension; the multi-item tray pose has its own.
IOBOX_POSITION_FIELDS: dict[str, dict[str, str]] = {
	"low": {"x": "pickup_low_x", "y": "pickup_low_y", "z": "z_extension", "stage": "stage_extension"},
	"high": {"x": "pickup_high_x", "y": "pickup_high_y", "z": "z_extension", "stage": "stage_extension"},
	"multi_item": {
		"x": "multi_item_x",
		"y": "multi_item_y",
		"z": "multi_item_z_extension",
		"stage": "multi_item_stage_extension",
	},
}


class IOBoxPositionResponse(BaseModel):
	pickup_low_x: float = Field(description="Position or extension in millimeters")
	pickup_low_y: float = Field(description="Position or extension in millimeters")
	pickup_high_x: float = Field(description="Position or extension in millimeters")
	pickup_high_y: float = Field(description="Position or extension in millimeters")
	z_extension: float = Field(description="Position or extension in millimeters")
	stage_extension: float = Field(description="Position or extension in millimeters")
	multi_item_x: float = Field(description="Position or extension in millimeters")
	multi_item_y: float = Field(description="Position or extension in millimeters")
	multi_item_z_extension: float = Field(description="Position or extension in millimeters")
	multi_item_stage_extension: float = Field(description="Position or extension in millimeters")
	warnings: Optional[list[str]] = None


IOBOX_CALIBRATION_DECIMAL_PLACES = 2


def _round_iobox_calibration_value(value: float) -> float:
	return round(float(value), IOBOX_CALIBRATION_DECIMAL_PLACES)


def _round_iobox_position(position: IOBoxPositionResponse) -> IOBoxPositionResponse:
	position.pickup_low_x = _round_iobox_calibration_value(position.pickup_low_x)
	position.pickup_low_y = _round_iobox_calibration_value(position.pickup_low_y)
	position.pickup_high_x = _round_iobox_calibration_value(position.pickup_high_x)
	position.pickup_high_y = _round_iobox_calibration_value(position.pickup_high_y)
	position.z_extension = _round_iobox_calibration_value(position.z_extension)
	position.stage_extension = _round_iobox_calibration_value(position.stage_extension)
	position.multi_item_x = _round_iobox_calibration_value(position.multi_item_x)
	position.multi_item_y = _round_iobox_calibration_value(position.multi_item_y)
	position.multi_item_z_extension = _round_iobox_calibration_value(position.multi_item_z_extension)
	position.multi_item_stage_extension = _round_iobox_calibration_value(position.multi_item_stage_extension)
	return position


class IOBoxJogRequest(BaseModel):
	position: IOBoxPositionName
	axis: IOBoxAxis
	delta: float = Field(description="Signed displacement in millimeters")


class IOBoxSaveRequest(BaseModel):
	pickup_low_x: float = Field(description="Position or extension in millimeters")
	pickup_low_y: float = Field(description="Position or extension in millimeters")
	pickup_high_x: float = Field(description="Position or extension in millimeters")
	pickup_high_y: float = Field(description="Position or extension in millimeters")
	z_extension: float = Field(description="Position or extension in millimeters")
	stage_extension: float = Field(description="Position or extension in millimeters")
	multi_item_x: float = Field(description="Position or extension in millimeters")
	multi_item_y: float = Field(description="Position or extension in millimeters")
	multi_item_z_extension: float = Field(description="Position or extension in millimeters")
	multi_item_stage_extension: float = Field(description="Position or extension in millimeters")


class IOBoxDoorPositionResponse(BaseModel):
	close: float
	open: float


class IOBoxDoorPositionRequest(BaseModel):
	close: float = Field(ge=0, le=1000)
	open: float = Field(gt=0, le=1000)


class IOBoxDoorMoveRequest(IOBoxDoorPositionRequest):
	position: Literal["close", "open"]


_iobox_position: IOBoxPositionName = "low"
_iobox_current: Optional[IOBoxPositionResponse] = None


def _get_iobox_door_config(config):
	try:
		return config.robot.io.motors.by_name("door")
	except KeyError as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="IO door motor is not configured",
		) from exc


def _validate_iobox_door_positions(close: float, open: float) -> None:
	if close >= open:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="IO door close position must be less than open position",
		)


def _with_iobox_door_positions(door_config, close: float, open: float):
	config_dict = door_config.model_dump()
	config_dict["lower_lim"] = close
	config_dict["lim"] = open

	try:
		return type(door_config).model_validate(config_dict)
	except ValueError as exc:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid IO door positions: {exc}",
		) from exc


@router.get(
    "/iobox/door",
    response_model=IOBoxDoorPositionResponse,
    summary="Read IO door travel limits",
    responses=response_docs(errors=(401, 409, 503)),
)
async def get_iobox_door_position() -> IOBoxDoorPositionResponse:
	"""Read IO door travel limits.

	Returns configured closed/open positions in millimeters. Reading these limits does not
	move the door.
	"""
	config = get_root_config()
	door_config = _get_iobox_door_config(config)

	return IOBoxDoorPositionResponse(
		close=float(door_config.lower_lim),
		open=float(door_config.lim),
	)


@router.post(
    "/iobox/door/move",
    dependencies=[ROBOT_CONTROL],
    summary="Test an IO door position",
    responses=response_docs(model=DoorMoveResponse, errors=(400, 401, 403, 409, 503)),
)
async def move_iobox_door(request: IOBoxDoorMoveRequest):
	"""Test an IO door position.

	Moves the physical door to the selected open/closed position using the supplied travel
	values. Returns the target position in millimeters. Save separately to persist the
	tested limits.
	"""
	await ensure_laser_auto_calibration_not_running()
	_validate_iobox_door_positions(request.close, request.open)

	worker = await get_robot_worker_or_503()
	_ensure_ready(worker)

	io = worker._robot._io
	motor = io._md
	if motor is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="IO door motor is not configured",
		)

	temporary_config = _with_iobox_door_positions(motor._params, request.close, request.open)
	target = request.close if request.position == "close" else request.open
	original_config = motor._params

	try:
		async with manual_motion_guard("calibration_iobox_door_move", worker=worker):
			async with io._door_motion_lock:
				motor._params = temporary_config
				try:
					await motor.move_to_position(target)
				finally:
					motor._params = original_config
			await io.refresh_door_state(force=True)
	except (IOBoxError, MotorError, HardwareMotorError) as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=f"Failed to move IO door: {exc}",
		) from exc

	return {"success": True, "position": request.position, "position_mm": target}


@router.post(
    "/iobox/door/save",
    dependencies=[ROBOT_CONTROL],
    summary="Save IO door travel limits",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
async def save_iobox_door_position(request: IOBoxDoorPositionRequest):
	"""Save IO door travel limits.

	Persists the supplied closed/open positions in the active configuration. Use the move
	endpoint to test physical travel before saving.
	"""
	await ensure_laser_auto_calibration_not_running()
	_validate_iobox_door_positions(request.close, request.open)

	config = get_root_config()
	door_config = _get_iobox_door_config(config)
	updated_door_config = _with_iobox_door_positions(
		door_config,
		request.close,
		request.open,
	)

	motors = config.robot.io.motors.root
	for index, motor_config in enumerate(motors):
		if motor_config.motor_name == "door":
			motors[index] = updated_door_config
			break

	save_root_config(config)

	try:
		worker = await get_robot_worker()
		worker._robot.reload_parameters()
	except ValueError:
		pass

	return {"success": True}


def _load_iobox_calibration() -> IOBoxPositionResponse:
	global _iobox_current

	config = get_root_config()
	params = config.robot.tool.parameters

	_iobox_current = IOBoxPositionResponse(
		pickup_low_x=_round_iobox_calibration_value(params.PICKUP_POSITION_L[0]),
		pickup_low_y=_round_iobox_calibration_value(params.PICKUP_POSITION_L[1]),
		pickup_high_x=_round_iobox_calibration_value(params.PICKUP_POSITION_H[0]),
		pickup_high_y=_round_iobox_calibration_value(params.PICKUP_POSITION_H[1]),
		z_extension=_round_iobox_calibration_value(params.Z_EXTENSION_IO),
		stage_extension=_round_iobox_calibration_value(params.STAGE_EXTENSION_IO_BOX),
		multi_item_x=_round_iobox_calibration_value(params.PICKUP_POSITION_MULTI_ITEM[0]),
		multi_item_y=_round_iobox_calibration_value(params.PICKUP_POSITION_MULTI_ITEM[1]),
		multi_item_z_extension=_round_iobox_calibration_value(params.Z_EXTENSION_IO_MULTI_ITEM),
		multi_item_stage_extension=_round_iobox_calibration_value(params.STAGE_EXTENSION_IO_BOX_MULTI_ITEM),
	)

	return _iobox_current


@router.get(
    "/iobox",
    response_model=IOBoxPositionResponse,
    summary="Read IO box pickup calibration",
    responses=response_docs(errors=(401, 409, 503)),
)
@serialize_calibration_state
async def get_iobox_calibration() -> IOBoxPositionResponse:
	"""Read IO box pickup calibration.

	Returns configured low/high pickup and multi-item tray coordinates and tool extensions in
	millimeters. Does not move hardware.
	"""
	return _load_iobox_calibration()


async def _get_iobox_current_or_error() -> IOBoxPositionResponse:
	if _iobox_current is None:
		return _load_iobox_calibration()

	return _iobox_current


@router.post(
    "/iobox/move",
    dependencies=[ROBOT_CONTROL],
    response_model=IOBoxPositionResponse,
    summary="Move to an IO pickup position",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
@serialize_calibration_state
async def move_to_iobox_position(position: IOBoxPositionName) -> IOBoxPositionResponse:
	"""Move to an IO pickup position.

	Opens the IO door, faces the tool forward, and moves to the configured low, high, or
	multi-item point. Updates the in-memory IO calibration state returned by the endpoint.
	"""
	global _iobox_position, _iobox_current

	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	iobox_current = await _get_iobox_current_or_error()

	tool = worker._robot._tool
	io = worker._robot._io

	fields = IOBOX_POSITION_FIELDS[position]
	x = getattr(iobox_current, fields["x"])
	y = getattr(iobox_current, fields["y"])

	async with manual_motion_guard("calibration_iobox_move", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			await _retract_tool_for_move(tool, worker)
			await tool.rotate_to_position("front")
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e, axes=["rotate"])

		try:
			await io.open_door()
		except (IOBoxError, MotorError, HardwareMotorError) as e:
			logging.getLogger(__name__).warning(f"Door open failed (continuing): {e}")

		try:
			await tool.move(AxisCoordinate(x, y))
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e)
	_iobox_position = position
	iobox_current.warnings = None

	return iobox_current


@router.post(
    "/iobox/jog",
    dependencies=[ROBOT_CONTROL],
    response_model=IOBoxPositionResponse,
    summary="Jog an IO pickup position",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
@serialize_calibration_state
async def jog_iobox_axis(request: IOBoxJogRequest) -> IOBoxPositionResponse:
	"""Jog an IO pickup position.

	Moves the selected low, high, or multi-item axis by signed `delta` millimeters. Pickup
	low and high share one Z and stage extension; the multi-item pose has its own. Updates
	in-memory calibration; call the IO save endpoint to persist changes.
	"""
	global _iobox_current, _iobox_position

	await ensure_laser_auto_calibration_not_running()

	iobox_current = await _get_iobox_current_or_error()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool
	_iobox_position = request.position

	field = IOBOX_POSITION_FIELDS[request.position][request.axis]
	current_value = getattr(iobox_current, field)

	warnings: list[str] = []

	try:
		async with manual_motion_guard("calibration_iobox_jog", worker=worker):
			if request.axis == "x":
				if tool._mx is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="X motor not configured",
					)
				await _clear_motors_for_move(tool, gantry=True)
				new_value = _clamp_value(current_value + request.delta, tool._mx._params.lim, "X axis", warnings)
				new_value = _round_iobox_calibration_value(new_value)
				await tool._mx.move_to_position(new_value)
			elif request.axis == "y":
				if tool._my is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="Y motor not configured",
					)
				await _clear_motors_for_move(tool, gantry=True)
				new_value = _clamp_value(current_value + request.delta, tool._my._params.lim, "Y axis", warnings)
				new_value = _round_iobox_calibration_value(new_value)
				await tool._my.move_to_position(new_value)
			elif request.axis == "z":
				if tool._mg is None:
					raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
				await _clear_motors_for_move(tool, gripper=True)
				new_value = _clamp_value(current_value + request.delta, tool._mg._params.lim, "Z axis", warnings)
				new_value = _round_iobox_calibration_value(new_value)
				grip_rpm = await tool.get_param("Z_MOVE_RPM")
				grip_accel = await tool.get_param("Z_MOVE_ACCEL")
				await tool._mg.move_to_position(new_value, rpm=grip_rpm, accel=grip_accel)
			else:
				if tool._ms is None:
					raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stage motor not configured")
				await _clear_motors_for_move(tool, stage=True)
				new_value = _clamp_value(current_value + request.delta, tool._ms._params.lim, "Stage", warnings)
				new_value = _round_iobox_calibration_value(new_value)
				await tool._ms.move_to_position(new_value)
			setattr(iobox_current, field, new_value)
	except (MotorError, HardwareMotorError) as e:
		axes: Optional[list[str]] = None
		if request.axis == "z":
			axes = ["z_axis"]
		elif request.axis == "stage":
			axes = ["stage"]
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e, axes=axes)

	iobox_current.warnings = warnings if warnings else None

	return iobox_current


@router.post(
    "/iobox/test",
    dependencies=[ROBOT_CONTROL],
    summary="Test the IO pickup sequence",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 500, 503)),
)
@serialize_calibration_state
async def test_iobox_sequence(with_gripper: bool = False):
	"""Test the IO pickup sequence.

	Executes the configured low-to-high pickup sequence and retracts the tool.
	`with_gripper=true` also operates the gripper. This moves physical hardware using the
	current in-memory calibration.
	"""
	global _iobox_current

	await ensure_laser_auto_calibration_not_running()

	iobox_current = await _get_iobox_current_or_error()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool
	io = worker._robot._io

	async with manual_motion_guard("calibration_iobox_test", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			await _retract_tool_for_move(tool, worker)
			await tool.rotate_to_position("front")
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

		try:
			await io.open_door()
		except (IOBoxError, MotorError, HardwareMotorError) as e:
			logging.getLogger(__name__).warning(f"Door open failed (continuing): {e}")

		try:
			await tool.move(AxisCoordinate(iobox_current.pickup_low_x, iobox_current.pickup_low_y))
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

		stage_ext = iobox_current.stage_extension
		grip_ext = iobox_current.z_extension
		grip_rpm = await tool.get_param("Z_MOVE_RPM")
		grip_accel = await tool.get_param("Z_MOVE_ACCEL")
		stage_nudge = await tool.get_param("STAGE_NUDGE")

		if tool._ms is None or tool._mg is None:
			raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Motors not configured")

		if stage_nudge is None:
			raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="STAGE_NUDGE not configured")

		stage_limit = _get_stage_limit(tool)
		_validate_stage_target(stage_ext, stage_limit, "STAGE_EXTENSION_IO_BOX")
		_validate_stage_target(stage_ext + stage_nudge, stage_limit, "STAGE_EXTENSION_IO_BOX + STAGE_NUDGE")

		try:
			await tool._ms.move_to_position(stage_ext)
			await tool._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
			await tool._ms.move_to_position(stage_ext + stage_nudge)

			if with_gripper is True:
				require_grip(await tool._gs.open(), "io test grip", is_enabled=tool.is_grip_detection_enabled)

			await tool.move(AxisCoordinate(iobox_current.pickup_high_x, iobox_current.pickup_high_y))
			await tool.retract()

			if with_gripper is True:
				await tool.release("io test release")
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e)
	return {"success": True}


@router.post(
    "/iobox/test/multi-item",
    dependencies=[ROBOT_CONTROL],
    summary="Test the multi-item tray cycle",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
@serialize_calibration_state
async def test_iobox_multi_item_cycle():
	"""Test the multi-item tray cycle.

	With a tray waiting in the IO box, releases the gripper, moves to the multi-item pose,
	extends, grips the tray, retracts it into the tool, then extends again, releases it, and
	retracts empty. This moves physical hardware using the current in-memory calibration.
	"""
	await ensure_laser_auto_calibration_not_running()

	iobox_current = await _get_iobox_current_or_error()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool
	io = worker._robot._io

	if tool._ms is None or tool._mg is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Motors not configured")

	stage_ext = iobox_current.multi_item_stage_extension
	grip_ext = iobox_current.multi_item_z_extension
	_validate_stage_target(stage_ext, _get_stage_limit(tool), "STAGE_EXTENSION_IO_BOX_MULTI_ITEM")
	grip_rpm = await tool.get_param("Z_MOVE_RPM")
	grip_accel = await tool.get_param("Z_MOVE_ACCEL")

	async def extend() -> None:
		await tool._ms.move_to_position(stage_ext)
		await tool._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)

	async with manual_motion_guard("calibration_iobox_multi_item_test", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
			await _retract_tool_for_move(tool, worker)
			await tool.rotate_to_position("front")
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

		try:
			await io.open_door()
		except (IOBoxError, MotorError, HardwareMotorError) as e:
			logging.getLogger(__name__).warning(f"Door open failed (continuing): {e}")

		try:
			await tool.release("multi-item test start release")
			await tool.move(AxisCoordinate(iobox_current.multi_item_x, iobox_current.multi_item_y))
			await extend()
			require_grip(await tool._gs.open(), "tray grip", is_enabled=tool.is_grip_detection_enabled)
			await tool.retract()
			require_grip(await tool._gs.read_state(), "tray retract", is_enabled=tool.is_grip_detection_enabled)
			await extend()
			await tool.release("multi-item test release")
			await tool.retract_empty()
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Multi-item test failed: {str(e)}", e)
	return {"success": True}


@router.post(
    "/iobox/save",
    dependencies=[ROBOT_CONTROL],
    summary="Save IO pickup calibration",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
@serialize_calibration_state
async def save_iobox_calibration(request: IOBoxSaveRequest):
	"""Save IO pickup calibration.

	Persists pickup coordinates and extensions, then closes/homes the tool and homes the IO
	door. This save operation also moves hardware; a homing failure can occur after
	configuration has been written.
	"""
	global _iobox_current

	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	stage_limit = _get_stage_limit(worker._robot._tool)
	config = get_root_config()
	stage_nudge = config.robot.tool.parameters.STAGE_NUDGE
	_validate_stage_target(request.stage_extension, stage_limit, "STAGE_EXTENSION_IO_BOX")
	_validate_stage_target(request.stage_extension + stage_nudge, stage_limit, "STAGE_EXTENSION_IO_BOX + STAGE_NUDGE")
	_validate_stage_target(request.multi_item_stage_extension, stage_limit, "STAGE_EXTENSION_IO_BOX_MULTI_ITEM")

	async with manual_motion_guard("calibration_iobox_save_home", worker=worker):
		params_dict = config.robot.tool.parameters.model_dump()
		params_dict["PICKUP_POSITION_L"] = (int(request.pickup_low_x), int(request.pickup_low_y))
		params_dict["PICKUP_POSITION_H"] = (int(request.pickup_high_x), int(request.pickup_high_y))
		params_dict["Z_EXTENSION_IO"] = _round_iobox_calibration_value(request.z_extension)
		params_dict["STAGE_EXTENSION_IO_BOX"] = _round_iobox_calibration_value(request.stage_extension)
		params_dict["PICKUP_POSITION_MULTI_ITEM"] = (int(request.multi_item_x), int(request.multi_item_y))
		params_dict["Z_EXTENSION_IO_MULTI_ITEM"] = _round_iobox_calibration_value(request.multi_item_z_extension)
		params_dict["STAGE_EXTENSION_IO_BOX_MULTI_ITEM"] = _round_iobox_calibration_value(
			request.multi_item_stage_extension
		)

		config.robot.tool.parameters = Parameters.model_validate(params_dict)
		save_root_config(config)
		worker._robot.reload_parameters()

		try:
			await worker._robot._tool.release("save home release")
		except GripVerificationError as e:
			# Homing would drag what the gripper still holds.
			await _raise_motor_error(worker, f"Gripper did not release before homing: {str(e)}", e)
		except Exception:
			pass

		try:
			await worker._robot._tool.home()
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Failed to home tool: {str(e)}", e)

		try:
			await worker._robot._io.home_door()
		except Exception:
			pass

		_iobox_current = _round_iobox_position(
			IOBoxPositionResponse(
				pickup_low_x=request.pickup_low_x,
				pickup_low_y=request.pickup_low_y,
				pickup_high_x=request.pickup_high_x,
				pickup_high_y=request.pickup_high_y,
				z_extension=request.z_extension,
				stage_extension=request.stage_extension,
				multi_item_x=request.multi_item_x,
				multi_item_y=request.multi_item_y,
				multi_item_z_extension=request.multi_item_z_extension,
				multi_item_stage_extension=request.multi_item_stage_extension,
			)
		)
	worker.clear_needs_home()
	await worker.get_status_snapshot(force_refresh=True)

	return {"success": True}


class RotationAnglesResponse(BaseModel):
	front: float
	back: float


class RotateSaveRequest(BaseModel):
	front: float
	back: float


@router.get(
    "/rotation",
    response_model=RotationAnglesResponse,
    summary="Read tool orientation positions",
    responses=response_docs(errors=(401, 409, 503)),
)
async def get_rotation_angles() -> RotationAnglesResponse:
	"""Read tool orientation positions.

	Returns the configured front/back rotation positions used by the tool. Does not rotate
	hardware.
	"""
	config = get_root_config()
	params = config.robot.tool.parameters

	return RotationAnglesResponse(
		front=params.ROTATION_FRONT,
		back=params.ROTATION_BACK,
	)


@router.post(
    "/rotation/rotate",
    dependencies=[ROBOT_CONTROL],
    summary="Test tool rotation",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
async def rotate_to_angle(angle: float):
	"""Test tool rotation.

	Rotates to the requested angle, clamped to the configured motor limits. Positions the
	stage as needed for rotation and restores its safe X position. Does not persist
	front/back calibration. Blocked by the rotation clearance guard (e.g. the tool is
	extended) with a 409 that names the unsafe axis; a misconfigured machine returns 400.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool
	if tool._mr is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Rotation motor not configured")

	rotation_limit = tool._mr._params.lim
	clamped_angle = max(0, min(rotation_limit, angle))
	try:
		async with manual_motion_guard("calibration_rotation", worker=worker):
			await _clear_motors_for_move(tool, gantry=True, stage=True, rotation=True)
			if await tool.is_rotation_at_position(clamped_angle):
				return {"success": True}
			# A rotation is only safe once both tool axes are retracted; retract instead of
			# rejecting a rotation that starts from an extended tool.
			await _retract_tool_for_move(tool, worker, blocked_message="Rotation blocked")
			original_x = await tool.ensure_safe_rotation_x_position()
			try:
				await tool.ensure_safe_rotation_stage_extension()
				await tool._mr.move_to_position(clamped_angle)
			finally:
				await tool.restore_safe_rotation_x_position(original_x)
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Rotation failed: {str(e)}", e)
	except MovementError as e:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=_build_error_detail(
				f"Rotation blocked: {str(e)}",
				code="rotation_blocked",
			),
		) from e
	except ConfigError as e:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=_build_error_detail(f"Invalid rotation configuration: {str(e)}", code="config_error"),
		) from e

	return {"success": True}


@router.post(
    "/rotation/save",
    dependencies=[ROBOT_CONTROL],
    summary="Save tool orientation positions",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 409, 503)),
)
async def save_rotation_angles(request: RotateSaveRequest):
	"""Save tool orientation positions.

	Persists the front/back rotation values in the active configuration. Test rotation
	separately before saving.
	"""
	await ensure_laser_auto_calibration_not_running()

	config = get_root_config()

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["ROTATION_FRONT"] = request.front
	params_dict["ROTATION_BACK"] = request.back

	config.robot.tool.parameters = Parameters.model_validate(params_dict)
	save_root_config(config)

	try:
		worker = await get_robot_worker()
		worker._robot.reload_parameters()
	except ValueError:
		pass

	return {"success": True}


class ShelfStageExtensionResponse(BaseModel):
	front: float
	back: float


class ShelfStageExtensionSaveRequest(BaseModel):
	front: float
	back: float


@router.get(
    "/shelf",
    response_model=ShelfStageExtensionResponse,
    summary="Read shelf stage extensions",
    responses=response_docs(errors=(401, 409, 503)),
)
async def get_shelf_stage_extension() -> ShelfStageExtensionResponse:
	"""Read shelf stage extensions.

	Returns configured front/back shelf extension distances in millimeters. Does not move
	the stage.
	"""
	config = get_root_config()
	params = config.robot.tool.parameters

	return ShelfStageExtensionResponse(
		front=params.STAGE_EXTENSION_SHELF_FRONT,
		back=params.STAGE_EXTENSION_SHELF_BACK,
	)


@router.post(
    "/shelf/save",
    dependencies=[ROBOT_CONTROL],
    summary="Save shelf stage extensions",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 409, 503)),
)
async def save_shelf_stage_extension(request: ShelfStageExtensionSaveRequest):
	"""Save shelf stage extensions.

	Persists front/back extension distances after checking the configured stage limit. Does
	not execute a test move.
	"""
	await ensure_laser_auto_calibration_not_running()

	config = get_root_config()

	stage_limit: float | None = None
	try:
		worker = await get_robot_worker()
		stage_limit = _get_stage_limit(worker._robot._tool)
	except ValueError:
		stage_limit = _get_stage_limit_from_config(config)

	if stage_limit is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Stage motor limit unavailable",
		)

	_validate_stage_target(request.front, stage_limit, "STAGE_EXTENSION_SHELF_FRONT")
	_validate_stage_target(request.back, stage_limit, "STAGE_EXTENSION_SHELF_BACK")

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["STAGE_EXTENSION_SHELF_FRONT"] = request.front
	params_dict["STAGE_EXTENSION_SHELF_BACK"] = request.back

	config.robot.tool.parameters = Parameters.model_validate(params_dict)
	save_root_config(config)

	try:
		worker = await get_robot_worker()
		worker._robot.reload_parameters()
	except ValueError:
		pass

	return {"success": True}


class StageMoveRequest(BaseModel):
	position: float


@router.post(
    "/stage/move",
    dependencies=[ROBOT_CONTROL],
    summary="Test a stage extension",
    responses=response_docs(model=StageMoveResponse, errors=(401, 403, 409, 503)),
)
async def move_stage(request: StageMoveRequest):
	"""Test a stage extension.

	Moves the stage toward the requested position in millimeters, applying configured
	limits. Returns the actual target and any warnings. Does not persist shelf extension
	settings.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool

	if tool._ms is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Stage motor not configured",
		)

	stage_limit = tool._ms._params.lim

	warnings: list[str] = []
	target = _clamp_value(request.position, stage_limit, "Stage", warnings)

	try:
		async with manual_motion_guard("calibration_stage", worker=worker):
			await _clear_motors_for_move(tool, stage=True)
			await tool._ms.move_to_position(target)
			await tool._ms.stop()
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

	return {"position": target, "warnings": warnings if warnings else None}


@router.post(
    "/abort",
    dependencies=[ROBOT_CONTROL],
    summary="Abort manual calibration and home the tool",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 409, 503)),
)
async def abort_calibration():
	"""Abort manual calibration and home the tool.

	Homes the tool and refreshes worker status. This recovery action itself moves hardware
	and is not an emergency stop. Automatic laser calibration must be aborted through its
	own endpoint.
	"""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	tool = worker._robot._tool

	try:
		async with manual_motion_guard("calibration_abort", worker=worker, allow_tray_on_tool=True):
			await tool.home()
		worker.clear_needs_home()
		await worker.get_status_snapshot(force_refresh=True)
		return {"success": True}
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Abort failed: {str(e)}", e)
