from __future__ import annotations

import asyncio
from typing import Any

import cv2
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from openshelf_os.config.models import IOBoxSizerConfig
from openshelf_os_server.core.iobox_sizer import (
	get_iobox_sizer_or_error,
	get_iobox_sizer_status,
	measure_iobox_size,
	reload_iobox_sizer_config,
)
from openshelf_os_server.settings import get_root_config, save_root_config
from .auth_deps import get_current_user, require_permissions

router = APIRouter(tags=["iobox-sizer"])


class TofBlankCalibrationRequest(BaseModel):
	samples: int = Field(5, ge=1, le=100)


def _unavailable_error(exc: Exception) -> HTTPException:
	return HTTPException(
		status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
		detail={"message": str(exc), "code": "iobox_sizer_unavailable"},
	)


@router.get("/status", dependencies=[Depends(get_current_user)])
async def status_endpoint() -> dict[str, Any]:
	return get_iobox_sizer_status()


@router.post("/measure", dependencies=[Depends(get_current_user)])
async def measure_endpoint() -> dict[str, Any]:
	try:
		measurement = await measure_iobox_size()
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	return {"success": True, "measurement": measurement.model_dump(mode="json")}


@router.get("/config", dependencies=[Depends(get_current_user)])
async def get_config_endpoint() -> dict[str, Any]:
	return get_root_config().robot.io.sizer.model_dump(mode="json")


@router.put(
	"/config",
	dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
)
async def update_config_endpoint(config: IOBoxSizerConfig) -> dict[str, Any]:
	root_config = get_root_config()
	root_config.robot.io.sizer = config
	save_root_config(root_config)
	await reload_iobox_sizer_config()
	return {"success": True, "config": config.model_dump(mode="json"), "status": get_iobox_sizer_status()}


@router.post(
	"/calibration/tof-blank",
	dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
)
async def calibrate_tof_blank_endpoint(request: TofBlankCalibrationRequest | None = None) -> dict[str, Any]:
	samples = 5 if request is None else request.samples
	try:
		service = get_iobox_sizer_or_error()
		blank_values = await asyncio.to_thread(service.calibrate_tof_blank, samples)
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	root_config = get_root_config()
	root_config.robot.io.sizer.calibration.tof_blank_mm = blank_values
	save_root_config(root_config)
	await reload_iobox_sizer_config()
	return {"success": True, "tof_blank_mm": blank_values}


@router.post(
	"/calibration/camera-blank",
	dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
)
async def capture_camera_blank_endpoint() -> dict[str, Any]:
	try:
		service = get_iobox_sizer_or_error()
		path = await asyncio.to_thread(service.capture_camera_blank)
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	root_config = get_root_config()
	root_config.robot.io.sizer.detection.blank_frame_path = path
	save_root_config(root_config)
	return {"success": True, "blank_frame_path": path}


@router.get("/frame.jpg", dependencies=[Depends(get_current_user)])
async def frame_endpoint() -> Response:
	try:
		service = get_iobox_sizer_or_error()
		frame = await asyncio.to_thread(service.capture_frame)
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
	if success is False:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail={"message": "failed to encode frame", "code": "frame_encode_failed"},
		)

	return Response(content=buffer.tobytes(), media_type="image/jpeg")
