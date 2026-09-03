from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import ConfigError, MotorError, MovementError, ToolError
from openshelf_os.fridge import FridgeSafetyError
from openshelf_os.storage import StorageModuleCellLocation
from hardware.exceptions import MotorError as HardwareMotorError

from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.core.bin_storage import (
	bin_workflow_command_admission_guard,
	reserved_bin_locations,
)
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers.common import SuccessResponse
from openshelf_os_server.core.storage_module import side_to_s
from openshelf_os_server.routers.auth_deps import get_current_user
from openshelf_os_server.routers.calibration import (
	_clear_motors_for_move,
	_ensure_ready,
	_raise_motor_error,
)

logger = logging.getLogger(__name__)


async def ensure_laser_auto_calibration_not_running() -> None:
	if await is_laser_auto_calibration_running() is False:
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

router = APIRouter(prefix="/api/demo", tags=["demo"], dependencies=[Depends(get_current_user)])

_demo_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]
_stop_event: asyncio.Event = asyncio.Event()
_is_running: bool = False
_current_cell_index: Optional[int] = None
_total_cells: Optional[int] = None


class DemoCycleStatusResponse(BaseModel):
	is_running: bool
	current_cell_index: Optional[int] = None
	total_cells: Optional[int] = None


def _build_cell_list(robot) -> list[tuple[StorageModuleCellLocation, AxisCoordinate, Optional[float]]]:
	grid = robot._grid

	cells: list[tuple[StorageModuleCellLocation, AxisCoordinate, Optional[float]]] = []

	for location in robot._demo_cell_map.values():
		cal_coord = grid.get_cell_coordinate(location)
		coord = cal_coord.to_axis_coordinate()
		coord.s = side_to_s(location.side)
		nudge = robot._get_demo_nudge(location)
		cells.append((location, coord, nudge))

	return cells


async def _ensure_demo_cells_are_not_reserved(
	robot,
	cells: list[tuple[StorageModuleCellLocation, AxisCoordinate, Optional[float]]],
) -> None:
	"""Reject demo mappings that target a server- or runtime-reserved bin home."""
	mapped_locations = {
		(location.side.value, location.shelf_idx, location.cell_idx)
		for location, _, _ in cells
	}
	runtime_reserved: set[tuple[str, int, int]] = set()
	robot_reservations: object = getattr(robot, "_reserved_cell_locations", set())
	if isinstance(robot_reservations, set):
		for reservation in robot_reservations:
			if (
				isinstance(reservation, tuple)
				and len(reservation) == 3
				and isinstance(reservation[0], str)
				and isinstance(reservation[1], int)
				and isinstance(reservation[2], int)
			):
				runtime_reserved.add(reservation)

	async with AsyncSessionLocal() as session:
		server_reserved = await reserved_bin_locations(session)

	if mapped_locations.isdisjoint(server_reserved | runtime_reserved) is False:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="Demo cell map includes a reserved storage bin home",
		)


async def _run_demo_cycle(worker, cells: list[tuple[StorageModuleCellLocation, AxisCoordinate, Optional[float]]]) -> None:
	global _is_running, _current_cell_index, _total_cells

	robot = worker._robot

	try:
		while _stop_event.is_set() is False:
			for idx, (location, coord, nudge) in enumerate(cells):
				_current_cell_index = idx

				if _stop_event.is_set() is True:
					break

				await robot.io_place(coord, nudge_override=nudge, location=location)

				if _stop_event.is_set() is True:
					await robot.io_pick(coord, location=location)
					break

				await robot.io_pick(coord, location=location)

				if _stop_event.is_set() is True:
					break
	except asyncio.CancelledError:
		logger.info("demo_cycle_cancelled")
	except FridgeSafetyError as exc:
		logger.warning("demo_cycle_fridge_safety_rejected: %s", exc)
		await worker.get_status_snapshot(force_refresh=True)
	except (MotorError, MovementError, ToolError, HardwareMotorError) as exc:
		logger.error("demo_cycle_hardware_fault", exc_info=True)
		worker.set_needs_home(f"Demo cycle hardware fault: {exc}")
		await worker.get_status_snapshot(force_refresh=True)
	except Exception as exc:
		logger.error("demo_cycle_unexpected_error", exc_info=True)
		worker.set_needs_home(f"Demo cycle crashed: {exc}")
		await worker.get_status_snapshot(force_refresh=True)
	finally:
		worker._demo_cycling = False
		_is_running = False
		_current_cell_index = None
		_total_cells = None
		logger.info("demo_cycle_stopped")


@router.post("/cycle/start", response_model=SuccessResponse)
async def start_demo_cycle() -> SuccessResponse:
	global _demo_task, _stop_event, _is_running, _current_cell_index, _total_cells

	await ensure_laser_auto_calibration_not_running()

	if _is_running is True:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="Demo cycle is already running",
		)

	worker = await get_robot_worker_or_503()

	_ensure_ready(worker)

	robot = worker._robot

	if robot._demo_cell_map is None or len(robot._demo_cell_map) == 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="No demo cell map configured",
		)

	cells = _build_cell_list(robot)

	tool = robot._tool

	async with manual_motion_guard("demo_cycle_start", worker=worker, check_bin_workflow=True):
		# Resolve reservations under the same process-local admission gate that
		# serializes bin workflow starts with demo startup.
		async with bin_workflow_command_admission_guard():
			await _ensure_demo_cells_are_not_reserved(robot, cells)
		await _clear_motors_for_move(tool, gantry=True, gripper=True, stage=True, rotation=True)

		_stop_event = asyncio.Event()
		_is_running = True
		_current_cell_index = 0
		_total_cells = len(cells)
		worker._demo_cycling = True
		_demo_task = asyncio.create_task(_run_demo_cycle(worker, cells))

	return SuccessResponse()


async def shutdown_demo_cycle() -> None:
	global _demo_task

	_stop_event.set()

	if _demo_task is None:
		return

	_demo_task.cancel()
	try:
		await _demo_task
	except asyncio.CancelledError:
		pass
	finally:
		_demo_task = None


@router.post("/cycle/stop", response_model=SuccessResponse)
async def stop_demo_cycle() -> SuccessResponse:
	if _is_running is False:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="Demo cycle is not running",
		)

	_stop_event.set()

	return SuccessResponse()


@router.get("/cycle/status", response_model=DemoCycleStatusResponse)
async def get_demo_cycle_status() -> DemoCycleStatusResponse:
	return DemoCycleStatusResponse(
		is_running=_is_running,
		current_cell_index=_current_cell_index,
		total_cells=_total_cells,
	)


@router.post("/fridge/{position}", response_model=SuccessResponse)
async def move_demo_fridge_door(
	position: Literal["left", "center", "right"],
) -> SuccessResponse:
	await ensure_laser_auto_calibration_not_running()
	worker = await get_robot_worker_or_503()
	_ensure_ready(worker)

	fridge = getattr(worker._robot, "_fridge", None)
	if fridge is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Fridge door is not configured for this robot",
		)

	try:
		async with manual_motion_guard(
			f"demo_fridge_{position}",
			worker=worker,
			check_bin_workflow=True,
		):
			await fridge.move_door_to_named(position)
	except ConfigError as exc:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=str(exc),
		) from exc
	except FridgeSafetyError as exc:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=str(exc),
		) from exc
	except (MotorError, HardwareMotorError) as exc:
		await _raise_motor_error(
			worker,
			f"Fridge door movement failed: {exc}",
			exc,
			axes=["fridge_door"],
		)

	return SuccessResponse(message=f"fridge door moved to {position}")
