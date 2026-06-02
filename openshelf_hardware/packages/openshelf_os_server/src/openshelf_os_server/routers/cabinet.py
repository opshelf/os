from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from openshelf_os.robot.worker import RobotWorker
from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.routers.auth_deps import get_current_user, require_permissions
from openshelf_os_server.settings import get_settings

router = APIRouter(tags=["cabinet"])


class CabinetDoorState(BaseModel):
    top_closed: bool
    bottom_closed: bool
    closed: bool
    top_open: bool
    bottom_open: bool
    open: bool


class CabinetLockState(BaseModel):
    top_unlocked: bool
    bottom_unlocked: bool
    unlocked: bool
    top_locked: bool
    bottom_locked: bool
    locked: bool


class CabinetLockResponse(BaseModel):
    success: bool
    action: Literal["lock", "unlock"]
    doors: CabinetDoorState
    locks: CabinetLockState


class CabinetStatusResponse(BaseModel):
    doors: CabinetDoorState
    locks: CabinetLockState


async def check_robot_enabled() -> None:
    settings = get_settings()
    if settings.robot.enabled is True:
        return

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Robot is not enabled or configured",
    )


async def get_robot_worker_or_error() -> RobotWorker:
    try:
        return await get_robot_worker()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Robot is not initialized or failed to start",
        ) from exc


async def ensure_laser_auto_calibration_not_running() -> None:
    if await is_laser_auto_calibration_running() is False:
        return

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="laser auto calibration is running",
    )


@router.get("/status", dependencies=[Depends(get_current_user), Depends(check_robot_enabled)])
async def get_cabinet_status(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> CabinetStatusResponse:
    try:
        cabinet_status = worker._robot._cabinet.get_status()
        return CabinetStatusResponse(
            doors=CabinetDoorState(**cabinet_status["doors"]),
            locks=CabinetLockState(**cabinet_status["locks"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Cabinet status read failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"message": f"Cabinet status failed: {exc}", "code": "cabinet_status_failed"},
        ) from exc


@router.post("/lock", dependencies=[Depends(require_permissions("storage:access")), Depends(check_robot_enabled)])
async def control_cabinet_lock(
    action: Literal["lock", "unlock"],
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> CabinetLockResponse:
    try:
        await ensure_laser_auto_calibration_not_running()
        async with manual_motion_guard(f"cabinet_lock:{action}", worker=worker):
            if action == "lock":
                cabinet_status = await worker._robot._cabinet.lock()
            else:
                cabinet_status = await worker._robot._cabinet.unlock()

            return CabinetLockResponse(
                success=True,
                action=action,
                doors=CabinetDoorState(**cabinet_status["doors"]),
                locks=CabinetLockState(**cabinet_status["locks"]),
            )
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Cabinet lock control failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": f"Cabinet lock {action} failed: {exc}",
                "code": "cabinet_lock_control_failed",
            },
        ) from exc
