from __future__ import annotations

import logging
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from perpetual_os1.common import AxisCoordinate
from perpetual_os1.storage import StorageModuleSide, CalibrationCoordinate
from perpetual_os1.exceptions import IOBoxError, MotorError
from hardware.exceptions import MotorError as HardwareMotorError

from perpetual_os1_server.core.robot import get_robot_worker
from perpetual_os1_server.routers.auth_deps import get_current_user
from perpetual_os1_server.settings import get_root_config, save_root_config

router = APIRouter(prefix="/api/calibration", tags=["calibration"], dependencies=[Depends(get_current_user)])


class PositionResponse(BaseModel):
	x: float
	y: float
	z_offset: float


class MoveRequest(BaseModel):
	side: Literal["left_module", "right_module"]
	shelf_idx: int
	cell_idx: int


class JogRequest(BaseModel):
	axis: Literal["x", "y", "z"]
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


_current_position: Optional[PositionResponse] = None
_current_side: int = 0


def _parse_side(side: str) -> StorageModuleSide:
	if side == "left_module":
		return StorageModuleSide.LEFT
	return StorageModuleSide.RIGHT


def _side_to_s(side: str) -> int:
	if side == "left_module":
		return 0
	return 1


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


@router.post("/move", response_model=PositionResponse)
async def move_to_cell(request: MoveRequest) -> PositionResponse:
	global _current_position, _current_side

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
	shelves = grid.get_side(side_enum)

	if request.shelf_idx < 0 or request.shelf_idx >= len(shelves):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid shelf_idx: {request.shelf_idx}",
		)

	shelf = shelves[request.shelf_idx]

	if request.cell_idx < 0 or request.cell_idx >= len(shelf.cells):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid cell_idx: {request.cell_idx}",
		)

	cell = shelf.cells[request.cell_idx]

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

	await tool.move(coord)

	_current_side = coord.s
	_current_position = PositionResponse(
		x=coord.x,
		y=coord.y,
		z_offset=coord.z_offset,
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

	tool = worker._robot._tool

	new_x = _current_position.x
	new_y = _current_position.y
	new_z = _current_position.z_offset

	if request.axis == "x":
		new_x += request.delta
	elif request.axis == "y":
		new_y += request.delta
	elif request.axis == "z":
		new_z += request.delta

	if request.axis in ("x", "y"):
		coord = AxisCoordinate(
			x=new_x,
			y=new_y,
			s=_current_side,
			z_offset=new_z,
		)
		await tool.move(coord)

	_current_position = PositionResponse(
		x=new_x,
		y=new_y,
		z_offset=new_z,
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

	tool = worker._robot._tool

	coord = AxisCoordinate(
		x=_current_position.x,
		y=_current_position.y,
		s=_current_side,
		z_offset=_current_position.z_offset,
	)

	await tool.pick(coord)
	await tool.place(coord)

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
	shelves = grid.get_side(side_enum)

	if request.shelf_idx < 0 or request.shelf_idx >= len(shelves):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid shelf_idx: {request.shelf_idx}",
		)

	shelf = shelves[request.shelf_idx]

	if request.cell_idx < 0 or request.cell_idx >= len(shelf.cells):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid cell_idx: {request.cell_idx}",
		)

	new_cal = CalibrationCoordinate(
		x=request.x,
		y=request.y,
		s=_side_to_s(request.side),
		z_offset=request.z_offset,
	)

	shelf.cells[request.cell_idx].calibration_point = new_cal

	grid.save()

	return {"success": True}


@router.post("/place")
async def place_at_cell(request: MoveRequest):
	"""Place a cell at the specified location (assumes cell is already on tool)."""
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
	shelves = grid.get_side(side_enum)

	if request.shelf_idx < 0 or request.shelf_idx >= len(shelves):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid shelf_idx: {request.shelf_idx}",
		)

	shelf = shelves[request.shelf_idx]

	if request.cell_idx < 0 or request.cell_idx >= len(shelf.cells):
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid cell_idx: {request.cell_idx}",
		)

	cell = shelf.cells[request.cell_idx]

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

	await tool.place(coord)

	return {"success": True}


class IOBoxPositionResponse(BaseModel):
	pickup_low_x: float
	pickup_low_y: float
	pickup_high_x: float
	pickup_high_y: float
	gripper_extension: float
	slide_extension: float
	io_stage_offset: float


class IOBoxJogRequest(BaseModel):
	position: Literal["low", "high"]
	axis: Literal["x", "y", "z", "stage"]
	delta: float


class IOBoxSaveRequest(BaseModel):
	pickup_low_x: float
	pickup_low_y: float
	pickup_high_x: float
	pickup_high_y: float
	gripper_extension: float
	slide_extension: float
	io_stage_offset: float


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
		gripper_extension=params.GRIPPER_EXTENSION_PLACE,
		slide_extension=params.SLIDE_EXTENSION_FRONT,
		io_stage_offset=params.IO_STAGE_OFFSET,
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
		await tool.rotate_to_position("front")
	except (MotorError, HardwareMotorError) as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Motor error: {str(e)}",
		) from e

	try:
		await io.open_door()
	except (IOBoxError, MotorError, HardwareMotorError) as e:
		logging.getLogger(__name__).warning(f"Door open failed (continuing): {e}")

	try:
		await tool.move(AxisCoordinate(x, y))
	except (MotorError, HardwareMotorError) as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Motor error: {str(e)}",
		) from e

	_iobox_position = position

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

	tool = worker._robot._tool
	_iobox_position = request.position

	try:
		if request.axis == "x":
			if request.position == "low":
				_iobox_current.pickup_low_x += request.delta
				await tool.move(AxisCoordinate(_iobox_current.pickup_low_x, _iobox_current.pickup_low_y))
			else:
				_iobox_current.pickup_high_x += request.delta
				await tool.move(AxisCoordinate(_iobox_current.pickup_high_x, _iobox_current.pickup_high_y))
		elif request.axis == "y":
			if request.position == "low":
				_iobox_current.pickup_low_y += request.delta
				await tool.move(AxisCoordinate(_iobox_current.pickup_low_x, _iobox_current.pickup_low_y))
			else:
				_iobox_current.pickup_high_y += request.delta
				await tool.move(AxisCoordinate(_iobox_current.pickup_high_x, _iobox_current.pickup_high_y))
		elif request.axis == "z":
			if tool._mg is None:
				raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
			current_z = await tool._mg.get_position()
			z_limit = tool._mg._params.lim
			if current_z < 0 or current_z > z_limit:
				current_z = _iobox_current.gripper_extension
			new_z = current_z + request.delta
			_iobox_current.gripper_extension = new_z
			grip_rpm = await tool.get_param("GRIPPER_MOVE_RPM")
			grip_accel = await tool.get_param("GRIPPER_MOVE_ACCEL")
			await tool._mg.move_to_position(new_z, rpm=grip_rpm, accel=grip_accel)
		elif request.axis == "stage":
			if tool._ms is None:
				raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Stage motor not configured")
			_iobox_current.io_stage_offset += request.delta
			slide_ext = _iobox_current.slide_extension
			await tool._ms.move_to_position(slide_ext + _iobox_current.io_stage_offset)
	except (MotorError, HardwareMotorError) as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Motor error: {str(e)}",
		) from e

	return _iobox_current


@router.post("/iobox/test")
async def test_iobox_sequence():
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

	tool = worker._robot._tool
	io = worker._robot._io

	try:
		await tool.rotate_to_position("front")
	except (MotorError, HardwareMotorError) as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Motor error: {str(e)}",
		) from e

	try:
		await io.open_door()
	except (IOBoxError, MotorError, HardwareMotorError) as e:
		logging.getLogger(__name__).warning(f"Door open failed (continuing): {e}")

	try:
		await tool.move(AxisCoordinate(_iobox_current.pickup_low_x, _iobox_current.pickup_low_y))
	except (MotorError, HardwareMotorError) as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Motor error: {str(e)}",
		) from e

	slide_ext = _iobox_current.slide_extension
	grip_ext = _iobox_current.gripper_extension
	io_offset = _iobox_current.io_stage_offset
	grip_rpm = await tool.get_param("GRIPPER_MOVE_RPM")
	grip_accel = await tool.get_param("GRIPPER_MOVE_ACCEL")

	if tool._ms is None or tool._mg is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Motors not configured")

	try:
		await tool._ms.move_to_position(slide_ext + io_offset)
		await tool._mg.move_to_position(grip_ext, rpm=grip_rpm, accel=grip_accel)
		await tool.move(AxisCoordinate(_iobox_current.pickup_high_x, _iobox_current.pickup_high_y))
		await tool.retract()
	except (MotorError, HardwareMotorError) as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Motor error: {str(e)}",
		) from e

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

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["PICKUP_POSITION_L"] = (int(request.pickup_low_x), int(request.pickup_low_y))
	params_dict["PICKUP_POSITION_H"] = (int(request.pickup_high_x), int(request.pickup_high_y))
	params_dict["GRIPPER_EXTENSION_PLACE"] = request.gripper_extension
	params_dict["SLIDE_EXTENSION_FRONT"] = request.slide_extension
	params_dict["IO_STAGE_OFFSET"] = request.io_stage_offset

	from perpetual_os1.common import Parameters
	config.robot.tool.parameters = Parameters.model_validate(params_dict)

	save_root_config(config)

	await worker._robot._tool.home()

	_iobox_current = IOBoxPositionResponse(
		pickup_low_x=request.pickup_low_x,
		pickup_low_y=request.pickup_low_y,
		pickup_high_x=request.pickup_high_x,
		pickup_high_y=request.pickup_high_y,
		gripper_extension=request.gripper_extension,
		slide_extension=request.slide_extension,
		io_stage_offset=request.io_stage_offset,
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
		front=params.FRONT,
		back=params.BACK,
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

	tool = worker._robot._tool
	if tool._mr is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Rotation motor not configured")

	await tool._mr.move_to_position(angle)

	return {"success": True}


@router.post("/rotation/save")
async def save_rotation_angles(request: RotateSaveRequest):
	config = get_root_config()

	params_dict = config.robot.tool.parameters.model_dump()
	params_dict["FRONT"] = request.front
	params_dict["BACK"] = request.back

	from perpetual_os1.common import Parameters
	config.robot.tool.parameters = Parameters.model_validate(params_dict)

	save_root_config(config)

	return {"success": True}


@router.get("/debug/motor30-registers")
async def debug_motor30_registers():
	"""Probe motor 30 registers to find correct position addresses."""
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	tool = worker._robot._tool
	if tool._mg is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
	controller = tool._mg._controller
	motor_id = 30
	mpr = 1.9992
	ppr = 10000

	results = {}

	register_pairs = [
		(0x602C, 0x602D, "CANopen style (current)"),
		(0x602D, 0x602C, "CANopen reversed"),
		(0x0050, 0x0051, "Variant A"),
		(0x0051, 0x0050, "Variant A reversed"),
		(0x6028, 0x6029, "Variant B"),
		(0x6030, 0x6031, "Variant C"),
	]

	for reg_h, reg_l, desc in register_pairs:
		try:
			val_h = await controller.read_holding_registers(motor_id, reg_h, 1)
			val_l = await controller.read_holding_registers(motor_id, reg_l, 1)

			if val_h is not None and val_l is not None:
				combined = (val_h << 16) | val_l
				pos_mm = (combined / ppr) * mpr

				results[desc] = {
					"reg_high": f"0x{reg_h:04X}",
					"reg_low": f"0x{reg_l:04X}",
					"val_high": val_h,
					"val_low": val_l,
					"combined_pulses": combined,
					"position_mm": round(pos_mm, 4),
				}
			else:
				results[desc] = {"error": "read returned None"}
		except Exception as e:
			results[desc] = {"error": str(e)}

	return results


@router.get("/debug/motor30-status-scan")
async def debug_motor30_status_scan():
	"""Scan motor 30 registers to find correct status register."""
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	tool = worker._robot._tool
	if tool._mg is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
	controller = tool._mg._controller
	motor_id = 30

	results = {}

	registers_to_scan = [
		0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A,
		0x48, 0x49, 0x4A, 0x4B, 0x4C, 0x4D, 0x4E, 0x4F,
		0x50, 0x51, 0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A,
		0x1003,
	]

	for reg in registers_to_scan:
		try:
			val = await controller.read_holding_registers(motor_id, reg, 1)
			if val is not None:
				results[f"0x{reg:04X}"] = {
					"decimal": val,
					"hex": f"0x{val:04X}",
					"binary": f"{val:016b}",
					"bit0": val & 1,
					"bit5": (val >> 5) & 1,
					"bit6": (val >> 6) & 1,
				}
			else:
				results[f"0x{reg:04X}"] = None
		except Exception as e:
			results[f"0x{reg:04X}"] = {"error": str(e)}

	return results


class SetPositionRequest(BaseModel):
	position_mm: float


@router.post("/debug/motor30-set-position")
async def debug_motor30_set_position(request: SetPositionRequest):
	"""Set motor 30 current position in mm."""
	try:
		worker = await get_robot_worker()
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Robot is not initialized",
		) from e

	tool = worker._robot._tool
	if tool._mg is None:
		raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Gripper motor not configured")
	controller = tool._mg._controller
	motor_id = 30
	mpr = 19.992
	ppr = 10000

	pulses = int((request.position_mm / mpr) * ppr)
	high = (pulses >> 16) & 0xFFFF
	low = pulses & 0xFFFF

	try:
		await controller.write_single_register(motor_id, 0x602C, high)
		await controller.write_single_register(motor_id, 0x602D, low)

		val_h = await controller.read_holding_registers(motor_id, 0x602C, 1)
		val_l = await controller.read_holding_registers(motor_id, 0x602D, 1)
		actual_pulses = (val_h << 16) | val_l
		actual_mm = (actual_pulses / ppr) * mpr

		return {
			"requested_mm": request.position_mm,
			"requested_pulses": pulses,
			"high": high,
			"low": low,
			"actual_pulses": actual_pulses,
			"actual_mm": round(actual_mm, 4),
		}
	except Exception as e:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail=f"Failed to set position: {e}",
		) from e
