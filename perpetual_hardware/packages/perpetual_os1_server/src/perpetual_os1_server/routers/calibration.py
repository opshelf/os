from __future__ import annotations

import asyncio
import logging
import re
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from perpetual_os1.common import AxisCoordinate
from perpetual_os1.laser_scan import ScanBounds
from perpetual_os1.storage import StorageModuleSide, CalibrationCoordinate, CellStatus
from perpetual_os1.exceptions import IOBoxError, MotorError
from hardware.exceptions import MotorError as HardwareMotorError

from perpetual_os1_server.core.robot import get_robot_worker
from perpetual_os1_server.routers.auth_deps import get_current_user
from perpetual_os1_server.settings import get_root_config, save_root_config

router = APIRouter(prefix="/api/calibration", tags=["calibration"], dependencies=[Depends(get_current_user)])

_MOTOR_ID_RE = re.compile(r"motor\s+(\d+)", re.IGNORECASE)


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


class JogRequest(BaseModel):
	axis: Literal["x", "y", "z", "stage"]
	delta: float


class TestRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	shelf_idx: int
	cell_idx: int


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


class ScanRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	x_min: Optional[float] = None
	x_max: Optional[float] = None
	y_min: Optional[float] = None
	y_max: Optional[float] = None
	step_mm: float = Field(default=5.0, gt=0)
	settle_ms: int = Field(default=50, ge=0)
	samples_per_point: int = Field(default=3, ge=1)
	laser_offset_x_mm: float = 0.0
	laser_offset_y_mm: float = 0.0
	wall_depth_tolerance_mm: float = Field(default=2.0, gt=0)
	expected_hole_width_mm: float = Field(default=26.0, gt=0)
	expected_hole_height_mm: float = Field(default=30.0, gt=0)
	hole_width_tolerance_mm: float = Field(default=10.0, gt=0)
	hole_height_tolerance_mm: float = Field(default=10.0, gt=0)
	min_vertical_wall_coverage_mm: float = Field(default=10.0, gt=0)
	min_horizontal_wall_coverage_mm: float = Field(default=30.0, gt=0)
	stage_extension_mm: Optional[float] = None
	apply: bool = True


class ScanStatusResponse(BaseModel):
	state: Literal["idle", "running", "completed", "failed", "cancelled"] = "idle"
	run_id: Optional[str] = None
	side: Optional[Literal["left_module", "right_module"]] = None
	bounds: Optional[ScanBounds] = None
	total_points: int = 0
	completed_points: int = 0
	current_x_mm: Optional[float] = None
	current_y_mm: Optional[float] = None
	current_depth_mm: Optional[float] = None
	error: Optional[str] = None
	result: Optional[dict] = None


_current_position: Optional[PositionResponse] = None
_current_side: int = 0
_current_side_name: str = "left_module"
_scan_task: Optional[asyncio.Task] = None
_scan_status = ScanStatusResponse()


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
		status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
		detail=_build_error_detail(
			message,
			code="motor_fault",
			needs_home=True,
			needs_home_axes=worker.needs_home_axes(),
		),
	) from exc


def _parse_side(side: str) -> StorageModuleSide:
	if side == "left_module":
		return StorageModuleSide.LEFT
	return StorageModuleSide.RIGHT


def _side_to_s(side: str) -> int:
	if side == "left_module":
		return 0
	return 1


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
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	return CalibrationStatusResponse(
		needs_home=worker.needs_home(),
		needs_home_reason=worker.needs_home_reason(),
		needs_home_axes=worker.needs_home_axes(),
	)


@router.get("/storage")
async def get_storage():
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	grid = worker._robot._grid
	return grid.model_dump()


def _derive_scan_bounds(grid, side: StorageModuleSide, request: ScanRequest) -> ScanBounds:
	explicit_bounds = [
		request.x_min,
		request.x_max,
		request.y_min,
		request.y_max,
	]

	if all(value is not None for value in explicit_bounds):
		return ScanBounds(
			x_min=float(request.x_min),
			x_max=float(request.x_max),
			y_min=float(request.y_min),
			y_max=float(request.y_max),
		)

	x_values: list[float] = []
	y_values: list[float] = []

	for shelf in grid.get_side(side):
		for cell in shelf.cells:
			if cell.calibration_point is None:
				continue
			x_values.append(float(cell.calibration_point.x))
			y_values.append(float(cell.calibration_point.y))

	if len(x_values) == 0 or len(y_values) == 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Scan bounds are required when the side has no calibrated cells",
		)

	margin_mm = max(request.step_mm * 2.0, 20.0)
	return ScanBounds(
		x_min=float(min(x_values) - margin_mm),
		x_max=float(max(x_values) + margin_mm),
		y_min=float(min(y_values) - margin_mm),
		y_max=float(max(y_values) + margin_mm),
	)


async def _did_scan_progress(
	completed_points: int,
	total_points: int,
	current_x_mm: float,
	current_y_mm: float,
	current_depth_mm: Optional[float],
) -> None:
	global _scan_status

	_scan_status.completed_points = completed_points
	_scan_status.total_points = total_points
	_scan_status.current_x_mm = current_x_mm
	_scan_status.current_y_mm = current_y_mm
	_scan_status.current_depth_mm = current_depth_mm


async def _run_scan_task(worker, request: ScanRequest, bounds: ScanBounds) -> None:
	global _scan_status, _scan_task

	try:
		result = await worker._robot.scan_storage(
			side=request.side,
			bounds=bounds,
			step_mm=request.step_mm,
			settle_ms=request.settle_ms,
			samples_per_point=request.samples_per_point,
			laser_offset_x_mm=request.laser_offset_x_mm,
			laser_offset_y_mm=request.laser_offset_y_mm,
			wall_depth_tolerance_mm=request.wall_depth_tolerance_mm,
			expected_hole_width_mm=request.expected_hole_width_mm,
			expected_hole_height_mm=request.expected_hole_height_mm,
			hole_width_tolerance_mm=request.hole_width_tolerance_mm,
			hole_height_tolerance_mm=request.hole_height_tolerance_mm,
			min_vertical_wall_coverage_mm=request.min_vertical_wall_coverage_mm,
			min_horizontal_wall_coverage_mm=request.min_horizontal_wall_coverage_mm,
			stage_extension_mm=request.stage_extension_mm,
			apply=request.apply,
			progress_callback=_did_scan_progress,
		)
		_scan_status.state = "completed"
		_scan_status.run_id = result.run_id
		_scan_status.result = result.model_dump(mode="json")
	except asyncio.CancelledError:
		_scan_status.state = "cancelled"
		_scan_status.error = None
		raise
	except Exception as e:
		_scan_status.state = "failed"
		_scan_status.error = str(e)
	finally:
		_scan_task = None


@router.post("/scan", response_model=ScanStatusResponse)
async def start_scan(request: ScanRequest) -> ScanStatusResponse:
	global _scan_status, _scan_task

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	if _scan_task is not None and _scan_task.done() is False:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="A calibration scan is already running",
		)

	if getattr(worker, "_current_command", None) is not None:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="Robot is busy processing another command",
		)

	bounds = _derive_scan_bounds(worker._robot._grid, _parse_side(request.side), request)
	x_positions = int(round((bounds.x_max - bounds.x_min) / request.step_mm)) + 1
	y_positions = int(round((bounds.y_max - bounds.y_min) / request.step_mm)) + 1
	total_points = max(1, x_positions * y_positions)

	_scan_status = ScanStatusResponse(
		state="running",
		run_id=None,
		side=request.side,
		bounds=bounds,
		total_points=total_points,
		completed_points=0,
		error=None,
		result=None,
	)

	_scan_task = asyncio.create_task(_run_scan_task(worker, request, bounds))
	return _scan_status


@router.get("/scan/status", response_model=ScanStatusResponse)
async def get_scan_status() -> ScanStatusResponse:
	return _scan_status


@router.post("/scan/abort", response_model=ScanStatusResponse)
async def abort_scan() -> ScanStatusResponse:
	global _scan_task, _scan_status

	if _scan_task is None or _scan_task.done() is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="No calibration scan is running",
		)

	_scan_task.cancel()
	_scan_status.state = "cancelled"
	_scan_status.error = None
	return _scan_status


@router.post("/move", response_model=PositionResponse)
async def move_to_cell(request: MoveRequest) -> PositionResponse:
	global _current_position, _current_side, _current_side_name

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	grid = worker._robot._grid
	tool = worker._robot._tool

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

	try:
		await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True)
		await tool.move(coord)

		if tool._mg is not None:
			grip_ext = await tool.get_param("Z_EXTENSION_PLACE")
			if grip_ext is not None:
				grip_rpm = await tool.get_param("Z_MOVE_RPM")
				grip_accel = await tool.get_param("Z_MOVE_ACCEL")
				target_z = grip_ext + coord.z_offset
				await tool._mg.move_to_position(target_z, rpm=grip_rpm, accel=grip_accel)
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Failed to move to cell: {str(e)}", e)

	slide_param = "STAGE_EXTENSION_SHELF_FRONT" if request.side == "left_module" else "STAGE_EXTENSION_SHELF_BACK"
	slide_ext = await tool.get_param(slide_param)

	if slide_ext is None:
		fallback_param = "SLIDE_EXTENSION_FRONT" if request.side == "left_module" else "SLIDE_EXTENSION_BACK"
		slide_ext = await tool.get_param(fallback_param)

	_current_side = coord.s
	_current_side_name = request.side
	_current_position = PositionResponse(
		x=coord.x,
		y=coord.y,
		z_offset=coord.z_offset,
		slide_extension=slide_ext or 0,
	)

	return _current_position


@router.post("/jog", response_model=PositionResponse)
async def jog_axis(request: JogRequest) -> PositionResponse:
	global _current_position

	if _current_position is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="No active calibration session. Move to a cell first.",
		)

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

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
		if request.axis == "x":
			await _clear_motors_for_move(tool, gantry=True)
			new_x = _clamp_value(new_x + request.delta, x_limit, "X axis", warnings)
			await tool._mx.move_to_position(new_x)
		elif request.axis == "y":
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
async def test_gripper(request: TestRequest):
	global _current_position, _current_side

	if _current_position is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="No active calibration session. Move to a cell first.",
		)

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	tool = worker._robot._tool

	try:
		await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
		await tool.retract()
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Failed to retract: {str(e)}", e)

	coord = AxisCoordinate(
		x=_current_position.x,
		y=_current_position.y,
		s=_current_side,
		z_offset=_current_position.z_offset,
	)

	try:
		await tool.pick(coord)
		await tool.place(coord)
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Test failed: {str(e)}", e)

	return {"success": True}


@router.post("/save")
async def save_calibration(request: SaveRequest):
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	grid = worker._robot._grid

	side_enum = _parse_side(request.side)
	shelf = _get_shelf(grid, side_enum, request.shelf_idx)
	cell = _get_cell(shelf, request.cell_idx)

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
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	tool = worker._robot._tool

	if tool._ms is None or tool._mg is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Tool motors not configured",
		)

	try:
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


@router.get("/iobox", response_model=IOBoxPositionResponse)
async def get_iobox_calibration() -> IOBoxPositionResponse:
	global _iobox_current

	config = get_root_config()
	params = config.robot.tool.parameters

	_iobox_current = IOBoxPositionResponse(
		pickup_low_x=params.PICKUP_POSITION_L[0],
		pickup_low_y=params.PICKUP_POSITION_L[1],
		pickup_high_x=params.PICKUP_POSITION_H[0],
		pickup_high_y=params.PICKUP_POSITION_H[1],
		z_extension=params.Z_EXTENSION_IO,
		stage_extension=params.STAGE_EXTENSION_IO_BOX,
	)

	return _iobox_current


@router.post("/iobox/move", response_model=IOBoxPositionResponse)
async def move_to_iobox_position(position: Literal["low", "high"]) -> IOBoxPositionResponse:
	global _iobox_position, _iobox_current

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	if _iobox_current is None:
		await get_iobox_calibration()
	if _iobox_current is None:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to initialize IOBox calibration",
		)

	tool = worker._robot._tool
	io = worker._robot._io

	if position == "low":
		x = _iobox_current.pickup_low_x
		y = _iobox_current.pickup_low_y
	else:
		x = _iobox_current.pickup_high_x
		y = _iobox_current.pickup_high_y

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
	_iobox_current.warnings = None

	return _iobox_current


@router.post("/iobox/jog", response_model=IOBoxPositionResponse)
async def jog_iobox_axis(request: IOBoxJogRequest) -> IOBoxPositionResponse:
	global _iobox_current, _iobox_position

	if _iobox_current is None:
		await get_iobox_calibration()
	if _iobox_current is None:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to initialize IOBox calibration",
		)

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	tool = worker._robot._tool
	_iobox_position = request.position

	x_limit = tool._mx._params.lim if tool._mx is not None else 1000
	y_limit = tool._my._params.lim if tool._my is not None else 1500

	warnings: list[str] = []

	try:
		if request.axis == "x":
			await _clear_motors_for_move(tool, gantry=True)
			if request.position == "low":
				new_x = _clamp_value(_iobox_current.pickup_low_x + request.delta, x_limit, "X axis", warnings)
				await tool._mx.move_to_position(new_x)
				_iobox_current.pickup_low_x = new_x
			else:
				new_x = _clamp_value(_iobox_current.pickup_high_x + request.delta, x_limit, "X axis", warnings)
				await tool._mx.move_to_position(new_x)
				_iobox_current.pickup_high_x = new_x
		elif request.axis == "y":
			await _clear_motors_for_move(tool, gantry=True)
			if request.position == "low":
				new_y = _clamp_value(_iobox_current.pickup_low_y + request.delta, y_limit, "Y axis", warnings)
				await tool._my.move_to_position(new_y)
				_iobox_current.pickup_low_y = new_y
			else:
				new_y = _clamp_value(_iobox_current.pickup_high_y + request.delta, y_limit, "Y axis", warnings)
				await tool._my.move_to_position(new_y)
				_iobox_current.pickup_high_y = new_y
		elif request.axis == "z":
			if tool._mg is None:
				raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
			await _clear_motors_for_move(tool, gripper=True)
			current_z = await tool._mg.get_position()
			z_limit = tool._mg._params.lim
			if current_z < 0 or current_z > z_limit:
				current_z = _iobox_current.z_extension
			new_z = _clamp_value(current_z + request.delta, z_limit, "Z axis", warnings)
			grip_rpm = await tool.get_param("Z_MOVE_RPM")
			grip_accel = await tool.get_param("Z_MOVE_ACCEL")
			await tool._mg.move_to_position(new_z, rpm=grip_rpm, accel=grip_accel)
			_iobox_current.z_extension = new_z
		elif request.axis == "stage":
			if tool._ms is None:
				raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stage motor not configured")
			await _clear_motors_for_move(tool, stage=True)
			stage_limit = tool._ms._params.lim
			new_stage = _clamp_value(_iobox_current.stage_extension + request.delta, stage_limit, "Stage", warnings)
			await tool._ms.move_to_position(new_stage)
			_iobox_current.stage_extension = new_stage
	except (MotorError, HardwareMotorError) as e:
		axes: Optional[list[str]] = None
		if request.axis == "z":
			axes = ["z_axis"]
		elif request.axis == "stage":
			axes = ["stage"]
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e, axes=axes)

	_iobox_current.warnings = warnings if warnings else None

	return _iobox_current


@router.post("/iobox/test")
async def test_iobox_sequence(with_gripper: bool = False):
	global _iobox_current

	if _iobox_current is None:
		await get_iobox_calibration()
	if _iobox_current is None:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Failed to initialize IOBox calibration",
		)

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	tool = worker._robot._tool
	io = worker._robot._io

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
		await tool.move(AxisCoordinate(_iobox_current.pickup_low_x, _iobox_current.pickup_low_y))
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

	stage_ext = _iobox_current.stage_extension
	grip_ext = _iobox_current.z_extension
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

		await tool.move(AxisCoordinate(_iobox_current.pickup_high_x, _iobox_current.pickup_high_y))
		await tool.retract()

		if with_gripper is True:
			await tool._gs.close()
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

	return {"success": True}


@router.post("/iobox/save")
async def save_iobox_calibration(request: IOBoxSaveRequest):
	global _iobox_current

	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	config = get_root_config()

	stage_limit = _get_stage_limit(worker._robot._tool)
	stage_nudge = config.robot.tool.parameters.STAGE_NUDGE
	_validate_stage_target(request.stage_extension, stage_limit, "STAGE_EXTENSION_IO_BOX")
	_validate_stage_target(request.stage_extension + stage_nudge, stage_limit, "STAGE_EXTENSION_IO_BOX + STAGE_NUDGE")

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["PICKUP_POSITION_L"] = (int(request.pickup_low_x), int(request.pickup_low_y))
	params_dict["PICKUP_POSITION_H"] = (int(request.pickup_high_x), int(request.pickup_high_y))
	params_dict["Z_EXTENSION_IO"] = request.z_extension
	params_dict["STAGE_EXTENSION_IO_BOX"] = request.stage_extension

	from perpetual_os1.common import Parameters
	config.robot.tool.parameters = Parameters.model_validate(params_dict)

	save_root_config(config)

	worker._robot.reload_parameters()

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

	_iobox_current = IOBoxPositionResponse(
		pickup_low_x=request.pickup_low_x,
		pickup_low_y=request.pickup_low_y,
		pickup_high_x=request.pickup_high_x,
		pickup_high_y=request.pickup_high_y,
		z_extension=request.z_extension,
		stage_extension=request.stage_extension,
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
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	tool = worker._robot._tool
	if tool._mr is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Rotation motor not configured")

	rotation_limit = tool._mr._params.lim
	clamped_angle = max(0, min(rotation_limit, angle))
	try:
		await _clear_motors_for_move(tool, rotation=True)
		await tool._mr.move_to_position(clamped_angle)
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Rotation failed: {str(e)}", e, axes=["rotate"])

	return {"success": True}


@router.post("/rotation/save")
async def save_rotation_angles(request: RotateSaveRequest):
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
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

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
		await _clear_motors_for_move(tool, stage=True)
		await tool._ms.move_to_position(target)
		await tool._ms.stop()
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Motor error: {str(e)}", e)

	return {"position": target, "warnings": warnings if warnings else None}


class CycleTestRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	shelf_idx: int
	cell_idx: int


@router.post("/cycle_test")
async def run_cycle_test(request: CycleTestRequest):
	"""Run a retrieve-store cycle test on a specific cell to verify calibration accuracy."""
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	_ensure_ready(worker)

	grid = worker._robot._grid
	robot = worker._robot
	tool = robot._tool

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

	try:
		await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)
		await robot.io_place(coord)
		await robot.io_pick(coord)
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Cycle test motor fault: {str(e)}", e)

	return {"success": True}


@router.post("/abort")
async def abort_calibration():
	"""Abort current calibration operation and home the tool."""
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	tool = worker._robot._tool

	try:
		await tool.home()
		worker.clear_needs_home()
		await worker.get_status_snapshot(force_refresh=True)
		return {"success": True}
	except (MotorError, HardwareMotorError) as e:
		await _raise_motor_error(worker, f"Abort failed: {str(e)}", e)
