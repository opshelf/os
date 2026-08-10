from __future__ import annotations

import json
import os
import socket
from typing import Any

import sentry_sdk
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from openshelf_os.device import get_device_id
from openshelf_os.operating_mode import should_enable_sentry
from openshelf_os_server.core.camera import camera_registry
from openshelf_os_server.core.logging_config import get_logger
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.database.models import User
from openshelf_os_server.settings import get_operating_mode
from openshelf_os_server.routers.auth_deps import get_current_user

logger = get_logger(__name__)

router = APIRouter(tags=["report"])


class ReportErrorRequest(BaseModel):
    message: str


class ReportErrorResponse(BaseModel):
    status: str


def _build_user_context(user: User) -> dict[str, Any]:
    return {
        "authenticated": True,
        "id": user.id,
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "role": user.role.name if user.role is not None else None,
    }


async def _build_robot_context() -> dict[str, Any]:
    try:
        worker = await get_robot_worker()
        status_json = await worker.get_status_snapshot()

        if status_json is None:
            return {"status": "no_status_available"}

        status = json.loads(status_json)
        robot_status = status.get("robot_status", {})

        return {
            "state": status.get("state"),
            "is_crashed": status.get("is_crashed"),
            "crash_reason": status.get("crash_reason"),
            "error_queue": status.get("error_queue", []),
            "gantry_position": robot_status.get("gantry"),
            "current_cell": robot_status.get("state", {}).get("current_cell"),
            "safety": robot_status.get("safety"),
        }
    except ValueError:
        return {"status": "not_initialized"}


def _build_system_context() -> dict[str, Any]:
	mode = get_operating_mode()

	return {
		"device_id": get_device_id(),
		"hostname": socket.gethostname(),
		"environment": mode.value,
		"version": os.getenv("APP_VERSION", "0.1.0"),
	}


async def _capture_camera_images() -> dict[str, bytes]:
    images: dict[str, bytes] = {}

    for camera_id, manager in camera_registry.items():
        if manager.is_running is True:
            frame = await manager.get_latest_frame()
            if frame is not None:
                images[camera_id] = frame

    return images


@router.post("/report_error", response_model=ReportErrorResponse, include_in_schema=False)
@router.post("/report-error", response_model=ReportErrorResponse)
async def submit_error_report(
    request: ReportErrorRequest,
    current_user: User = Depends(get_current_user),
) -> ReportErrorResponse:
	user_context = _build_user_context(current_user)
	robot_context = await _build_robot_context()
	system_context = _build_system_context()
	camera_images = await _capture_camera_images()

	device_id = str(system_context["device_id"])
	hostname = socket.gethostname()
	user_name = f"{current_user.first_name} {current_user.last_name}"
	mode = get_operating_mode()
	sentry_enabled = should_enable_sentry(mode)

	logger.info(
		"user_error_report_submitted",
		user=user_name,
		device_id=device_id,
		hostname=hostname,
		message_preview=request.message[:100],
		camera_count=len(camera_images),
	)

	if sentry_enabled is True and sentry_sdk.is_initialized() is True:
		with sentry_sdk.push_scope() as scope:
			scope.set_context("user_report", {"message": request.message})
			scope.set_context("user", user_context)
			scope.set_context("robot_state", robot_context)
			scope.set_context("system", system_context)
			scope.set_tag("report_type", "user_submitted")
			scope.set_tag("device_id", device_id)
			scope.set_tag("hostname", hostname)

			for name, image_bytes in camera_images.items():
				scope.add_attachment(bytes=image_bytes, filename=f"{name}.jpg")

			sentry_sdk.capture_message(
				f"User Report: {request.message}",
				level="error",
			)

	return ReportErrorResponse(status="ok")
