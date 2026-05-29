from __future__ import annotations

import asyncio
from typing import Any

import cv2
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from openshelf_os.config.models import IOBoxSizerConfig
from openshelf_os_server.core.iobox_sizer import (
	get_iobox_sizer_or_error,
	get_iobox_sizer_status,
	measure_iobox_size,
	reload_iobox_sizer_config,
)
from openshelf_os_server.core.iobox_sizer_history import build_config_snapshot, list_measurements, record_measurement
from openshelf_os_server.database.models import IOBoxSizerMeasurement, User
from openshelf_os_server.database.session import get_db
from openshelf_os_server.settings import get_root_config, save_root_config
from .auth_deps import get_current_user, require_permissions

router = APIRouter(tags=["iobox-sizer"])


class ManualMeasureRequest(BaseModel):
	save: bool = True


class TofBlankCalibrationRequest(BaseModel):
	samples: int = Field(5, ge=1, le=100)


def _row_to_dict(row: IOBoxSizerMeasurement) -> dict[str, Any]:
	return {
		"id": row.id,
		"created_at": row.created_at.isoformat(),
		"trigger": row.trigger,
		"observer_mode": row.observer_mode,
		"upca": row.upca,
		"item_name": row.item_name,
		"user_id": row.user_id,
		"request_correlation_id": row.request_correlation_id,
		"command_payload": row.command_payload,
		"human_dimensions": row.human_dimensions,
		"human_dimensions_source": row.human_dimensions_source,
		"measurement": row.measurement,
		"detected": row.detected,
		"success": row.success,
		"error_type": row.error_type,
		"error_message": row.error_message,
		"duration_ms": row.duration_ms,
		"config_snapshot": row.config_snapshot,
		"debug_image_path": row.debug_image_path,
	}


def _unavailable_error(exc: Exception) -> HTTPException:
	return HTTPException(
		status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
		detail={"message": str(exc), "code": "iobox_sizer_unavailable"},
	)


@router.get("/status", dependencies=[Depends(get_current_user)])
async def status_endpoint() -> dict[str, Any]:
	return get_iobox_sizer_status()


@router.post("/measure", dependencies=[Depends(get_current_user)])
async def measure_endpoint(
	request: ManualMeasureRequest | None = None,
	current_user: User = Depends(get_current_user),
	db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
	try:
		measurement = await measure_iobox_size()
	except Exception as exc:
		raise _unavailable_error(exc) from exc

	save = True if request is None else request.save
	if save is True:
		config = get_root_config().robot.io.sizer
		await record_measurement(
			db,
			trigger="manual",
			observer_mode=config.observer_mode_enabled,
			user_id=current_user.id,
			measurement=measurement,
			config_snapshot=build_config_snapshot(config),
		)

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


@router.post(
	"/reconnect",
	dependencies=[Depends(get_current_user), Depends(require_permissions("robot:control"))],
)
async def reconnect_endpoint() -> dict[str, Any]:
	await reload_iobox_sizer_config()
	return {"success": True, "status": get_iobox_sizer_status()}


@router.get("/measurements", dependencies=[Depends(get_current_user)])
async def list_measurements_endpoint(
	limit: int = Query(50, ge=1, le=500),
	offset: int = Query(0, ge=0),
	upca: str | None = Query(None),
	trigger: str | None = Query(None),
	db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
	rows = await list_measurements(db, limit=limit, offset=offset, upca=upca, trigger=trigger)
	return {"measurements": [_row_to_dict(row) for row in rows], "limit": limit, "offset": offset}


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
