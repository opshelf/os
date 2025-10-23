from __future__ import annotations

import asyncio
from typing import AsyncGenerator

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from perpetual_os1_server.core.camera import (
	get_camera_manager,
	start_camera_stream,
	stop_camera_stream,
)
from perpetual_os1_server.settings import get_settings
from .auth_deps import get_current_user_optional

from perpetual_os1_server.routers.common import SuccessResponse

router = APIRouter(tags=["camera"])


async def check_camera_enabled() -> None:
	settings = get_settings()

	if settings.camera.enabled is False:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Camera is not enabled or configured",
		)


async def mjpeg_stream_generator(request: Request) -> AsyncGenerator[bytes, None]:
	try:
		await start_camera_stream()
		manager = await get_camera_manager()

		black_frame_cache = None

		while True:
			if await request.is_disconnected() is True:
				break

			if manager.calibration_complete.is_set() is False:
				if black_frame_cache is None:
					black = np.zeros((900, 1950, 3), dtype=np.uint8)
					_, buffer = cv2.imencode('.jpg', black, [cv2.IMWRITE_JPEG_QUALITY, 85])
					black_frame_cache = buffer.tobytes()

				yield (
					b"--frame\r\n"
					b"Content-Type: image/jpeg\r\n\r\n" + black_frame_cache + b"\r\n"
				)

				await asyncio.sleep(0.05)
				continue

			frame_bytes = await manager.get_latest_frame()

			if frame_bytes is None:
				if black_frame_cache is None:
					black = np.zeros((900, 1950, 3), dtype=np.uint8)
					_, buffer = cv2.imencode('.jpg', black, [cv2.IMWRITE_JPEG_QUALITY, 85])
					black_frame_cache = buffer.tobytes()

				yield (
					b"--frame\r\n"
					b"Content-Type: image/jpeg\r\n\r\n" + black_frame_cache + b"\r\n"
				)

				await asyncio.sleep(0.05)
				continue

			yield (
				b"--frame\r\n"
				b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
			)

			await asyncio.sleep(1.0 / manager.target_fps)

	except asyncio.CancelledError:
		pass
	finally:
		await stop_camera_stream()


@router.get("")
async def stream_camera(
	request: Request,
	current_user: dict | None = Depends(get_current_user_optional),
) -> StreamingResponse:
	await check_camera_enabled()

	return StreamingResponse(
		mjpeg_stream_generator(request),
		media_type="multipart/x-mixed-replace; boundary=frame",
	)


@router.get("/status")
async def get_camera_status(
	current_user: dict | None = Depends(get_current_user_optional),
) -> dict[str, str | int | bool]:
	settings = get_settings()

	if settings.camera.enabled is False:
		return {
			"enabled": False,
			"message": "Camera is disabled in settings"
		}

	try:
		manager = await get_camera_manager()
		return {
			"enabled": True,
			"running": manager.is_running,
			"consumers": manager.consumer_count,
			"target_fps": manager.target_fps,
			"url": manager.camera_url,
			"has_frame": manager.latest_frame is not None
		}
	except ValueError:
		return {
			"enabled": True,
			"message": "Camera manager not initialized"
		}
