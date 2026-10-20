from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from openshelf_os.robot.worker import RobotWorker
from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.manual_motion import manual_motion_guard
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.errors import api_error
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


class CabinetLatchState(BaseModel):
    top_released: bool
    bottom_released: bool
    released: bool
    top_engaged: bool
    bottom_engaged: bool
    engaged: bool


class CabinetReleaseResponse(BaseModel):
    success: bool
    action: Literal["passive", "release"]
    doors: CabinetDoorState
    latches: CabinetLatchState


class CabinetStatusResponse(BaseModel):
    configured: bool = True
    doors: CabinetDoorState | None = None
    latches: CabinetLatchState | None = None

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


@router.get(
    "/status",
    dependencies=[Depends(get_current_user), Depends(check_robot_enabled)],
    summary="Read cabinet doors and latches",
    responses=response_docs(errors=(401, 503)),
)
async def get_cabinet_status(
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> CabinetStatusResponse:
    """Read cabinet doors and latches.

    Reports cabinet GPIO door/latch state. `configured=false` means this hardware is not
    configured, rather than a closed-door guarantee. Requires authentication and an enabled
    robot.
    """
    cabinet = worker._robot._cabinet
    if cabinet.is_configured() is False:
        return CabinetStatusResponse(configured=False)

    try:
        cabinet_status = cabinet.get_status()
        return CabinetStatusResponse(
            configured=True,
            doors=CabinetDoorState(**cabinet_status["doors"]),
            latches=CabinetLatchState(**cabinet_status["latches"]),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Cabinet status read failed: %s", exc)
        raise api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "cabinet_status_failed",
            "The cabinet status could not be read. Try again.",
        ) from exc


@router.post(
    "/release",
    dependencies=[Depends(require_permissions("storage:access")), Depends(check_robot_enabled)],
    summary="Control cabinet latch release",
    responses=response_docs(errors=(401, 403, 409, 503)),
)
async def control_cabinet_release(
    action: Literal["passive", "release"],
    worker: RobotWorker = Depends(get_robot_worker_or_error),
) -> CabinetReleaseResponse:
    """Control cabinet latch release.

    Requires `storage:access`. Sets the physical latch output to `release` or `passive` and
    returns updated door/latch state. Unconfigured GPIO returns `409`; hardware failures
    return `503`. Blocked during automatic laser calibration.
    """
    try:
        await ensure_laser_auto_calibration_not_running()
        cabinet = worker._robot._cabinet
        if cabinet.is_configured() is False:
            raise api_error(
                status.HTTP_409_CONFLICT,
                "cabinet_not_configured",
                "This cabinet cannot open its doors.",
            )

        async with manual_motion_guard(
            f"cabinet_release:{action}", worker=worker
        ):
            if action == "passive":
                cabinet_status = await cabinet.set_passive()
            else:
                cabinet_status = await cabinet.release()

            return CabinetReleaseResponse(
                success=True,
                action=action,
                doors=CabinetDoorState(**cabinet_status["doors"]),
                latches=CabinetLatchState(**cabinet_status["latches"]),
            )
    except HTTPException:
        raise
    except Exception as exc:
        logging.error("Cabinet release %s failed: %s", action, exc)
        raise api_error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "cabinet_release_control_failed",
            "The cabinet did not release. Try again.",
        ) from exc
