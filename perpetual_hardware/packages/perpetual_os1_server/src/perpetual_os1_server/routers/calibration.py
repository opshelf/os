from __future__ import annotations

from typing import Literal, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from perpetual_os1.common import AxisCoordinate
from perpetual_os1.storage import StorageModuleSide, CalibrationCoordinate

from perpetual_os1_server.core.robot import get_robot_worker

router = APIRouter(prefix="/api/calibration", tags=["calibration"])


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
