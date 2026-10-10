from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from openshelf_os.common import AxisCoordinate
from openshelf_os.exceptions import MotorError, MovementError, ToolError
from openshelf_os.storage import StorageModuleCellLocation
from hardware.exceptions import MotorError as HardwareMotorError

from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.routers.common import SuccessResponse
from openshelf_os_server.core.storage_module import side_to_s
from openshelf_os_server.routers.auth_deps import get_current_user, require_permissions
from openshelf_os_server.routers.calibration import _ensure_ready, _clear_motors_for_move

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


async def _run_demo_cycle(worker, cells: list[tuple[StorageModuleCellLocation, AxisCoordinate, Optional[float]]]) -> None:
	global _is_running, _current_cell_index, _total_cells

	robot = worker._robot

	try:
		while _stop_event.is_set() is False:
			for idx, (location, coord, nudge) in enumerate(cells):
				_current_cell_index = idx

				if _stop_event.is_set() is True:
					break

				await robot.io_place(coord, nudge_override=nudge)

				if _stop_event.is_set() is True:
					await robot.io_pick(coord)
					break

				await robot.io_pick(coord)

				if _stop_event.is_set() is True:
					break
	except asyncio.CancelledError:
		logger.info("demo_cycle_cancelled")
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


@router.post(
    "/cycle/start",
    dependencies=[Depends(require_permissions("robot:control"))],
    response_model=SuccessResponse,
    summary="Start the demonstration cycle",
    responses=response_docs(errors=(400, 401, 403, 409, 503)),
)
async def start_demo_cycle() -> SuccessResponse:
	"""Start the demonstration cycle.

	Starts background motion through the configured demo cell map. Requires an authenticated
	user and available hardware. Missing cells return `400`; an already running demo or
	conflicting motion returns `409`. Acceptance does not mean the cycle has finished.
	"""
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

	async with manual_motion_guard("demo_cycle_start", worker=worker):
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


@router.post(
    "/cycle/stop",
    dependencies=[Depends(require_permissions("robot:control"))],
    response_model=SuccessResponse,
    summary="Request demonstration stop",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def stop_demo_cycle() -> SuccessResponse:
	"""Request demonstration stop.

	Requests the running demo cycle to stop. Returns `409` when no demo is running. Poll
	demo status for completion; this is not a physical emergency stop.
	"""
	if _is_running is False:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="Demo cycle is not running",
		)

	_stop_event.set()

	return SuccessResponse()


@router.get(
    "/cycle/status",
    response_model=DemoCycleStatusResponse,
    summary="Read demonstration progress",
    responses=response_docs(errors=(401, 409, 503)),
)
async def get_demo_cycle_status() -> DemoCycleStatusResponse:
	"""Read demonstration progress.

	Reports whether the background demo is running, its current cell index, and configured
	cell count. Does not start or stop motion.
	"""
	return DemoCycleStatusResponse(
		is_running=_is_running,
		current_cell_index=_current_cell_index,
		total_cells=_total_cells,
	)
