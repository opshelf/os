from __future__ import annotations

import asyncio
import functools
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Literal, Optional, ParamSpec, TypeVar
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from perpetual_os1.common import AxisCoordinate
from perpetual_os1.storage import (
	CalibrationCoordinate,
	Cell,
	CellItem,
	CellStatus,
	Dimensions,
	Shelf,
	StorageModuleCellLocation,
	StorageModuleSide,
)
from perpetual_os1.exceptions import IOBoxError, MotorError, ToolError
from hardware.exceptions import MotorError as HardwareMotorError

from perpetual_os1_server.core.robot import get_robot_worker
from perpetual_os1_server.core.laser_auto_calibration import (
	LaserAutoCalibrationScope,
	LaserAutoCalibrationSnapshot,
	get_laser_auto_calibration_manager,
)
from perpetual_os1_server.core.manual_motion import manual_motion_guard
from perpetual_os1_server.routers.auth_deps import get_current_user
from perpetual_os1_server.core.storage_module import module_name_to_side, side_to_s
from perpetual_os1_server.settings import get_root_config, save_root_config

router = APIRouter(prefix="/api/calibration", tags=["calibration"], dependencies=[Depends(get_current_user)])

_MOTOR_ID_RE = re.compile(r"motor\s+(\d+)", re.IGNORECASE)
P = ParamSpec("P")
T = TypeVar("T")

_calibration_state_lock = asyncio.Lock()


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
		detail="laser auto calibration is running",
	)


async def get_robot_worker_or_503():
	try:
		return await get_robot_worker()
	except ValueError as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from exc
_DEFAULT_SHELF_HEIGHT = 200.0
_DEFAULT_SHELF_WIDTH = 480.0
_DEFAULT_DIVIDER_WIDTH = 12.0
_DEFAULT_CELL_LENGTH = 400.0


class PositionResponse(BaseModel):
	x: float
	y: float
	z_offset: float
	slide_extension: float
	warnings: Optional[list[str]] = None


class MoveRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	shelf_idx: int
	cell_idx: int


class MovePositionRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	x: float
	y: float
	z_offset: float = 0.0


class JogRequest(BaseModel):
	axis: Literal["x", "y", "z", "stage"]
	delta: float


class TestRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	shelf_idx: int
	cell_idx: int



def _build_test_location(request: TestRequest) -> StorageModuleCellLocation:
	return StorageModuleCellLocation(
		side=_parse_side(request.side),
		shelf_idx=request.shelf_idx,
		cell_idx=request.cell_idx,
	)


def _build_laser_auto_calibration_scope(
	side: Literal["left_module", "right_module"] | None,
	shelf_idx: int | None,
	cell_idx: int | None,
	mode: Literal["all", "io_box"] | None,
	include_io_box: bool | None,
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

	if mode == "io_box":
		if side is not None or shelf_idx is not None or cell_idx is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="side, shelf_idx, and cell_idx cannot be used with io_box mode",
			)

		return LaserAutoCalibrationScope(mode="io_box", include_io_box=True)

	if side is None:
		if shelf_idx is not None or cell_idx is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="side is required when shelf_idx or cell_idx is provided",
			)

		return LaserAutoCalibrationScope(mode="all", include_io_box=include_io_box is not False)

	if cell_idx is not None and shelf_idx is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="shelf_idx is required when cell_idx is provided",
		)

	if shelf_idx is None:
		return LaserAutoCalibrationScope(mode="side", side=side, include_io_box=include_io_box is True)

	if cell_idx is None:
		return LaserAutoCalibrationScope(mode="shelf", side=side, shelf_idx=shelf_idx, include_io_box=include_io_box is True)

	return LaserAutoCalibrationScope(mode="cell", side=side, shelf_idx=shelf_idx, cell_idx=cell_idx, include_io_box=include_io_box is True)


def _build_coordinate_for_location(grid, location: StorageModuleCellLocation) -> AxisCoordinate:
	calibration_point = grid.get_cell_coordinate(location)

	return AxisCoordinate(
		x=calibration_point.x,
		y=calibration_point.y,
		s=0 if location.side == StorageModuleSide.LEFT else 1,
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
	side: Literal["left_module", "right_module"]
	shelf_idx: int
	cell_idx: int
	x: float
	y: float
	z_offset: float
	status: Optional[str] = None


class CalibrationStatusResponse(BaseModel):
	needs_home: bool
	needs_home_reason: Optional[str] = None
	needs_home_axes: list[str] = Field(default_factory=list)


class LaserReadingResponse(BaseModel):
	distance_mm: float


_current_position: Optional[PositionResponse] = None
_current_side: int = 0
_current_side_name: str = "left_module"


def _build_error_detail(
	message: str,
	*,
	code: str,
	needs_home: bool = False,
	needs_home_axes: Optional[list[str]] = None,
	reason: Optional[str] = None,
) -> dict[str, object]:
	detail: dict[str, object] = {"message": message, "code": code}
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


def _parse_side(side: str) -> StorageModuleSide:
	return module_name_to_side(side)


def _side_to_s(side: str) -> int:
	return side_to_s(module_name_to_side(side))


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
		shelf_height=_DEFAULT_SHELF_HEIGHT,
		shelf_width=_DEFAULT_SHELF_WIDTH,
		cells=[],
		divider_width=_DEFAULT_DIVIDER_WIDTH,
	)


def _build_default_cell_template(shelf) -> Cell:
	return Cell(
		cell_idx=0,
		status=CellStatus.AVAILABLE,
		item=None,
		calibration_point=None,
		dimensions=Dimensions(
			width=max(float(shelf.shelf_width), 1.0),
			height=max(float(shelf.shelf_height), 1.0),
			length=_DEFAULT_CELL_LENGTH,
		),
		empty=False,
	)


def _get_shelf_template(grid, side: StorageModuleSide) -> Shelf:
	shelves = grid.get_side(side)

	if len(shelves) > 0:
		return shelves[-1]

	other_side = StorageModuleSide.RIGHT if side == StorageModuleSide.LEFT else StorageModuleSide.LEFT
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

	other_side = StorageModuleSide.RIGHT if side == StorageModuleSide.LEFT else StorageModuleSide.LEFT

	for candidate_shelf in reversed(grid.get_side(other_side)):
		if len(candidate_shelf.cells) > 0:
			return candidate_shelf.cells[-1]

	return _build_default_cell_template(shelf)


def _ensure_shelf(grid, side: StorageModuleSide, shelf_idx: int):
	shelf = _find_shelf(grid, side, shelf_idx)

	if shelf is not None:
		return shelf

	template = _get_shelf_template(grid, side)
	new_shelf = Shelf(
		shelf_idx=shelf_idx,
		shelf_height=template.shelf_height,
		shelf_width=template.shelf_width,
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
		empty=template.empty,
	)
	shelf.cells.append(new_cell)

	return new_cell


async def _move_tool_to_coordinate(
	worker,
	coord: AxisCoordinate,
	*,
	side_name: Literal["left_module", "right_module"],
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
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Failed to move to cell: {str(e)}", e)

	slide_extension = await _get_current_stage_extension(tool)

	_current_side = coord.s
	_current_side_name = side_name
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


@router.get("/status", response_model=CalibrationStatusResponse)
async def get_calibration_status() -> CalibrationStatusResponse:
	worker = await get_robot_worker_or_503()

	return CalibrationStatusResponse(
		needs_home=worker.needs_home(),
		needs_home_reason=worker.needs_home_reason(),
		needs_home_axes=worker.needs_home_axes(),
	)


@router.get("/storage")
async def get_storage():
	worker = await get_robot_worker_or_503()

	grid = worker._robot._grid
	return grid.model_dump()


@router.get("/laser/read", response_model=LaserReadingResponse)
async def read_laser() -> LaserReadingResponse:
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


@router.get("/laser/status", response_model=LaserAutoCalibrationSnapshot)
async def get_laser_auto_calibration_status() -> LaserAutoCalibrationSnapshot:
	manager = get_laser_auto_calibration_manager()
	return await manager.get_snapshot()


@router.post("/laser/abort", response_model=LaserAutoCalibrationSnapshot)
async def abort_laser_auto_calibration() -> LaserAutoCalibrationSnapshot:
	manager = get_laser_auto_calibration_manager()

	try:
		return await manager.abort()
	except RuntimeError as exc:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=str(exc),
		) from exc


@router.get("/laser/stream")
async def stream_laser_auto_calibration(
	request: Request,
	side: Literal["left_module", "right_module"] | None = None,
	shelf_idx: int | None = None,
	cell_idx: int | None = None,
	mode: Literal["all", "io_box"] | None = None,
	include_io_box: bool | None = None,
	subscribe_only: bool = False,
) -> EventSourceResponse:
	requested_scope = _build_laser_auto_calibration_scope(side, shelf_idx, cell_idx, mode, include_io_box)
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
				detail="Robot is busy processing another command",
			)

		if getattr(worker, "_demo_cycling", False) is True:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="Demo cycle is already running",
			)

		try:
			await manager.ensure_session_started(worker, requested_scope)
		except RuntimeError as exc:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
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
		finally:
			await manager.unsubscribe(queue)

	return EventSourceResponse(
		event_generator(),
		ping=15,
		send_timeout=30,
		headers={"Cache-Control": "no-cache"},
	)


@router.post("/move", response_model=PositionResponse)
@serialize_calibration_state
async def move_to_cell(request: MoveRequest) -> PositionResponse:
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	grid = worker._robot._grid

	side_enum = _parse_side(request.side)
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


@router.post("/move/position", response_model=PositionResponse)
@serialize_calibration_state
async def move_to_position(request: MovePositionRequest) -> PositionResponse:
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)
	tool = worker._robot._tool

	coord, warnings = await _build_clamped_manual_coordinate(tool, request)

	return await _move_tool_to_coordinate(worker, coord, side_name=request.side, warnings=warnings)


@router.post("/jog", response_model=PositionResponse)
@serialize_calibration_state
async def jog_axis(request: JogRequest) -> PositionResponse:
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


@router.post("/test")
@serialize_calibration_state
async def test_gripper(request: TestRequest):
	global _current_position, _current_side

	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	location = _build_test_location(request)
	grid = worker._robot._grid

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
			await tool.pick(coord)
			await tool.place(coord)
		except (MotorError, HardwareMotorError, ToolError) as e:
			await _raise_motor_error(worker, f"Test failed: {str(e)}", e)
	return {"success": True}


@router.post("/test/cycle")
async def test_cycle(request: TestRequest):
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	robot = worker._robot
	tool = robot._tool
	grid = robot._grid
	location = _build_test_location(request)

	try:
		cell = grid.get_cell(location)
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
	return {"success": True}


@router.post("/save")
async def save_calibration(request: SaveRequest):
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	grid = worker._robot._grid
	_validate_non_negative_index(request.shelf_idx, "shelf_idx")
	_validate_non_negative_index(request.cell_idx, "cell_idx")

	side_enum = _parse_side(request.side)
	shelf = _ensure_shelf(grid, side_enum, request.shelf_idx)
	cell = _ensure_cell(grid, side_enum, shelf, request.cell_idx)

	new_cal = CalibrationCoordinate(
		x=request.x,
		y=request.y,
		s=_side_to_s(request.side),
		z_offset=request.z_offset,
	)

	cell.calibration_point = new_cal

	if request.status is not None:
		try:
			new_status = CellStatus(request.status)
			cell.status = new_status
		except ValueError:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail=f"Invalid status: {request.status}. Must be one of: available, occupied, maintenance, disabled",
			)

	grid.save()

	return {"success": True}


@router.post("/home_tool_motors", response_model=CalibrationStatusResponse)
async def home_tool_motors() -> CalibrationStatusResponse:
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	tool = worker._robot._tool

	if tool._ms is None or tool._mg is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Tool motors not configured",
		)

	try:
		async with manual_motion_guard("calibration_home_tool_motors", worker=worker):
			await tool._ms.home()
			await tool._mg.home()
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Failed to home tool motors: {str(e)}", e, axes=["stage", "z_axis"])

	worker.clear_needs_home_axes(["stage", "z_axis"])
	await worker.get_status_snapshot(force_refresh=True)

	return CalibrationStatusResponse(
		needs_home=worker.needs_home(),
		needs_home_reason=worker.needs_home_reason(),
		needs_home_axes=worker.needs_home_axes(),
	)


class IOBoxPositionResponse(BaseModel):
	pickup_low_x: float
	pickup_low_y: float
	pickup_high_x: float
	pickup_high_y: float
	z_extension: float
	stage_extension: float
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
	return position


class IOBoxJogRequest(BaseModel):
	position: Literal["low", "high"]
	axis: Literal["x", "y", "z", "stage"]
	delta: float


class IOBoxSaveRequest(BaseModel):
	pickup_low_x: float
	pickup_low_y: float
	pickup_high_x: float
	pickup_high_y: float
	z_extension: float
	stage_extension: float


_iobox_position: Literal["low", "high"] = "low"
_iobox_current: Optional[IOBoxPositionResponse] = None


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
	)

	return _iobox_current


@router.get("/iobox", response_model=IOBoxPositionResponse)
@serialize_calibration_state
async def get_iobox_calibration() -> IOBoxPositionResponse:
	return _load_iobox_calibration()


async def _get_iobox_current_or_error() -> IOBoxPositionResponse:
	if _iobox_current is None:
		return _load_iobox_calibration()

	return _iobox_current


@router.post("/iobox/move", response_model=IOBoxPositionResponse)
@serialize_calibration_state
async def move_to_iobox_position(position: Literal["low", "high"]) -> IOBoxPositionResponse:
	global _iobox_position, _iobox_current

	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	iobox_current = await _get_iobox_current_or_error()

	tool = worker._robot._tool
	io = worker._robot._io

	if position == "low":
		x = iobox_current.pickup_low_x
		y = iobox_current.pickup_low_y
	else:
		x = iobox_current.pickup_high_x
		y = iobox_current.pickup_high_y

	async with manual_motion_guard("calibration_iobox_move", worker=worker):
		try:
			await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
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


@router.post("/iobox/jog", response_model=IOBoxPositionResponse)
@serialize_calibration_state
async def jog_iobox_axis(request: IOBoxJogRequest) -> IOBoxPositionResponse:
	global _iobox_current, _iobox_position

	await ensure_laser_auto_calibration_not_running()

	iobox_current = await _get_iobox_current_or_error()

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	tool = worker._robot._tool
	_iobox_position = request.position

	x_limit = tool._mx._params.lim if tool._mx is not None else 1000
	y_limit = tool._my._params.lim if tool._my is not None else 1500

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
				if request.position == "low":
					new_x = _clamp_value(iobox_current.pickup_low_x + request.delta, x_limit, "X axis", warnings)
					new_x = _round_iobox_calibration_value(new_x)
					await tool._mx.move_to_position(new_x)
					iobox_current.pickup_low_x = new_x
				else:
					new_x = _clamp_value(iobox_current.pickup_high_x + request.delta, x_limit, "X axis", warnings)
					new_x = _round_iobox_calibration_value(new_x)
					await tool._mx.move_to_position(new_x)
					iobox_current.pickup_high_x = new_x
			elif request.axis == "y":
				if tool._my is None:
					raise HTTPException(
						status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
						detail="Y motor not configured",
					)
				await _clear_motors_for_move(tool, gantry=True)
				if request.position == "low":
					new_y = _clamp_value(iobox_current.pickup_low_y + request.delta, y_limit, "Y axis", warnings)
					new_y = _round_iobox_calibration_value(new_y)
					await tool._my.move_to_position(new_y)
					iobox_current.pickup_low_y = new_y
				else:
					new_y = _clamp_value(iobox_current.pickup_high_y + request.delta, y_limit, "Y axis", warnings)
					new_y = _round_iobox_calibration_value(new_y)
					await tool._my.move_to_position(new_y)
					iobox_current.pickup_high_y = new_y
			elif request.axis == "z":
				if tool._mg is None:
					raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
				await _clear_motors_for_move(tool, gripper=True)
				z_limit = tool._mg._params.lim
				new_z = _clamp_value(iobox_current.z_extension + request.delta, z_limit, "Z axis", warnings)
				new_z = _round_iobox_calibration_value(new_z)
				grip_rpm = await tool.get_param("Z_MOVE_RPM")
				grip_accel = await tool.get_param("Z_MOVE_ACCEL")
				await tool._mg.move_to_position(new_z, rpm=grip_rpm, accel=grip_accel)
				iobox_current.z_extension = new_z
			elif request.axis == "stage":
				if tool._ms is None:
					raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stage motor not configured")
				await _clear_motors_for_move(tool, stage=True)
				stage_limit = tool._ms._params.lim
				new_stage = _clamp_value(iobox_current.stage_extension + request.delta, stage_limit, "Stage", warnings)
				new_stage = _round_iobox_calibration_value(new_stage)
				await tool._ms.move_to_position(new_stage)
				iobox_current.stage_extension = new_stage
	except (MotorError, HardwareMotorError) as e:
		axes: Optional[list[str]] = None
		if request.axis == "z":
			axes = ["z_axis"]
		elif request.axis == "stage":
			axes = ["stage"]
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e, axes=axes)

	iobox_current.warnings = warnings if warnings else None

	return iobox_current


@router.post("/iobox/test")
@serialize_calibration_state
async def test_iobox_sequence(with_gripper: bool = False):
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
				await tool._gs.open()

			await tool.move(AxisCoordinate(iobox_current.pickup_high_x, iobox_current.pickup_high_y))
			await tool.retract()

			if with_gripper is True:
				await tool._gs.close()
		except (MotorError, HardwareMotorError) as e:
			await _raise_motor_error(worker, f"Motor error: {str(e)}", e)
	return {"success": True}


@router.post("/iobox/save")
@serialize_calibration_state
async def save_iobox_calibration(request: IOBoxSaveRequest):
	global _iobox_current

	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	config = get_root_config()

	stage_limit = _get_stage_limit(worker._robot._tool)
	stage_nudge = config.robot.tool.parameters.STAGE_NUDGE
	_validate_stage_target(request.stage_extension, stage_limit, "STAGE_EXTENSION_IO_BOX")
	_validate_stage_target(request.stage_extension + stage_nudge, stage_limit, "STAGE_EXTENSION_IO_BOX + STAGE_NUDGE")

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["PICKUP_POSITION_L"] = (int(request.pickup_low_x), int(request.pickup_low_y))
	params_dict["PICKUP_POSITION_H"] = (int(request.pickup_high_x), int(request.pickup_high_y))
	params_dict["Z_EXTENSION_IO"] = _round_iobox_calibration_value(request.z_extension)
	params_dict["STAGE_EXTENSION_IO_BOX"] = _round_iobox_calibration_value(request.stage_extension)

	from perpetual_os1.common import Parameters
	config.robot.tool.parameters = Parameters.model_validate(params_dict)

	save_root_config(config)

	worker._robot.reload_parameters()

	async with manual_motion_guard("calibration_iobox_save_home", worker=worker):
		try:
			await worker._robot._tool._gs.close()
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
	worker.clear_needs_home()
	await worker.get_status_snapshot(force_refresh=True)

	_iobox_current = _round_iobox_position(
		IOBoxPositionResponse(
			pickup_low_x=request.pickup_low_x,
			pickup_low_y=request.pickup_low_y,
			pickup_high_x=request.pickup_high_x,
			pickup_high_y=request.pickup_high_y,
			z_extension=request.z_extension,
			stage_extension=request.stage_extension,
		)
	)

	return {"success": True}


class RotationAnglesResponse(BaseModel):
	front: float
	back: float


class RotateSaveRequest(BaseModel):
	front: float
	back: float


@router.get("/rotation", response_model=RotationAnglesResponse)
async def get_rotation_angles() -> RotationAnglesResponse:
	config = get_root_config()
	params = config.robot.tool.parameters

	return RotationAnglesResponse(
		front=params.ROTATION_FRONT,
		back=params.ROTATION_BACK,
	)


@router.post("/rotation/rotate")
async def rotate_to_angle(angle: float):
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
			await _clear_motors_for_move(tool, rotation=True)
			if await tool.is_rotation_at_position(clamped_angle):
				return {"success": True}
			original_x = await tool.ensure_safe_rotation_x_position()
			try:
				await tool.ensure_safe_rotation_stage_extension()
				await tool._mr.move_to_position(clamped_angle)
			finally:
				await tool.restore_safe_rotation_x_position(original_x)
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Rotation failed: {str(e)}", e, axes=["rotate"])

	return {"success": True}


@router.post("/rotation/save")
async def save_rotation_angles(request: RotateSaveRequest):
	await ensure_laser_auto_calibration_not_running()

	config = get_root_config()

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["ROTATION_FRONT"] = request.front
	params_dict["ROTATION_BACK"] = request.back

	from perpetual_os1.common import Parameters
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


@router.get("/shelf", response_model=ShelfStageExtensionResponse)
async def get_shelf_stage_extension() -> ShelfStageExtensionResponse:
	config = get_root_config()
	params = config.robot.tool.parameters

	return ShelfStageExtensionResponse(
		front=params.STAGE_EXTENSION_SHELF_FRONT,
		back=params.STAGE_EXTENSION_SHELF_BACK,
	)


@router.post("/shelf/save")
async def save_shelf_stage_extension(request: ShelfStageExtensionSaveRequest):
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

	from perpetual_os1.common import Parameters
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


@router.post("/stage/move")
async def move_stage(request: StageMoveRequest):
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


@router.post("/abort")
async def abort_calibration():
	"""Abort current calibration operation and home the tool."""
	await ensure_laser_auto_calibration_not_running()

	worker = await get_robot_worker_or_503()

	tool = worker._robot._tool

	try:
		async with manual_motion_guard("calibration_abort", worker=worker):
			await tool.home()
		worker.clear_needs_home()
		await worker.get_status_snapshot(force_refresh=True)
		return {"success": True}
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Abort failed: {str(e)}", e)
