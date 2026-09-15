from __future__ import annotations
from openshelf_os_server.documentation import response_docs, stream_docs

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

from openshelf_os.iobox_sizer.models import IOBoxSizerMeasurement
from openshelf_os.common import IOBoxSizerCameraConfig

class SizerStatusResponse(BaseModel):
    enabled: bool
    available: bool
    observer_mode_enabled: bool
    measure_on_store: bool
    camera: IOBoxSizerCameraConfig
    tof_channels: list[int]
    last_error: str | None
    last_error_type: str | None
    last_measurement_time: str | None


class SizerMeasurementResponse(BaseModel):
    success: bool
    measurement: IOBoxSizerMeasurement


class SizerConfigResponse(BaseModel):
    success: bool
    config: IOBoxSizerConfig
    status: SizerStatusResponse


class SizerBlankDistancesResponse(BaseModel):
    success: bool
    tof_blank_mm: list[float]


class SizerBlankFrameResponse(BaseModel):
    success: bool
    blank_frame_path: str


router = APIRouter(tags=["iobox-sizer"])


class TofBlankCalibrationRequest(BaseModel):
	samples: int = Field(5, ge=1, le=100)


def _unavailable_error(exc: Exception) -> HTTPException:
	return HTTPException(
		status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
		detail={"message": str(exc), "code": "iobox_sizer_unavailable"},
	)


@router.get(
    "/status",
    dependencies=[Depends(get_current_user)],
    summary="Read IO sizer status",
    responses=response_docs(model=SizerStatusResponse, errors=(401, 503)),
)
async def status_endpoint() -> dict[str, Any]:
	"""Read IO sizer status.

	Reports IO-box sizing availability, configuration state, and latest measurement status.
	Does not start a measurement. Requires authentication.
	"""
	return get_iobox_sizer_status()


@router.post(
    "/measure",
    dependencies=[Depends(get_current_user)],
    summary="Measure the IO box contents",
    responses=response_docs(model=SizerMeasurementResponse, errors=(401, 503)),
)
async def measure_endpoint() -> dict[str, Any]:
	"""Measure the IO box contents.

	Captures a sizing measurement and returns dimensions and observation details. Does not
	enroll or move an inventory item. Unavailable sensors return `503` with a structured
	error.
	"""
	try:
		measurement = await measure_iobox_size()
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	return {"success": True, "measurement": measurement.model_dump(mode="json")}


@router.get(
    "/config",
    dependencies=[Depends(get_current_user)],
    summary="Read IO sizer configuration",
    responses=response_docs(model=IOBoxSizerConfig, errors=(401, 503)),
)
async def get_config_endpoint() -> dict[str, Any]:
	"""Read IO sizer configuration.

	Returns the active IO sizer configuration, including sensor and calibration settings.
	Requires authentication.
	"""
	return get_root_config().robot.io.sizer.model_dump(mode="json")


@router.put(
    "/config",
    dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
    summary="Replace IO sizer configuration",
    responses=response_docs(model=SizerConfigResponse, errors=(401, 403, 503)),
)
async def update_config_endpoint(config: IOBoxSizerConfig) -> dict[str, Any]:
	"""Replace IO sizer configuration.

	Requires `robot:control`. Persists the supplied complete sizer configuration and reloads
	it, returning the new configuration and status. This is a replacement rather than a
	partial patch.
	"""
	root_config = get_root_config()
	root_config.robot.io.sizer = config
	save_root_config(root_config)
	await reload_iobox_sizer_config()
	return {"success": True, "config": config.model_dump(mode="json"), "status": get_iobox_sizer_status()}


@router.post(
    "/calibration/tof-blank",
    dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
    summary="Calibrate empty-box distance sensors",
    responses=response_docs(model=SizerBlankDistancesResponse, errors=(401, 403, 503)),
)
async def calibrate_tof_blank_endpoint(request: TofBlankCalibrationRequest | None = None) -> dict[str, Any]:
	"""Calibrate empty-box distance sensors.

	Requires `robot:control`. Samples the empty IO box, persists blank distance values, and
	reloads the sizer configuration. Prepare an empty box first; unavailable sensors return
	`503`.
	"""
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
    summary="Capture the empty-box camera reference",
    responses=response_docs(model=SizerBlankFrameResponse, errors=(401, 403, 503)),
)
async def capture_camera_blank_endpoint() -> dict[str, Any]:
	"""Capture the empty-box camera reference.

	Requires `robot:control`. Captures the camera blank used by sizing and returns its
	stored path. Prepare an empty IO box before calling; this changes calibration data.
	"""
	try:
		service = get_iobox_sizer_or_error()
		path = await asyncio.to_thread(service.capture_camera_blank)
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	return {"success": True, "blank_frame_path": path}


@router.get(
    "/frame.jpg",
    dependencies=[Depends(get_current_user)],
    summary="Read an IO sizer JPEG frame",
    responses=response_docs(
        errors=(401, 500, 503), existing=stream_docs("image/jpeg", "JPEG camera frame bytes.", None)
    ),
    response_class=Response,
)
async def frame_endpoint() -> Response:
	"""Read an IO sizer JPEG frame.

	Captures the current sizer camera frame and returns JPEG bytes. Does not return JSON on
	success. Unavailable hardware returns `503`; encoding failure returns `500`.
	"""
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
