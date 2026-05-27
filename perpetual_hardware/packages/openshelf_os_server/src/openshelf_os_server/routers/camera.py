from __future__ import annotations

import asyncio
from typing import AsyncGenerator

import cv2
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from openshelf_os_server.core.camera import (
	CameraManager,
	get_camera_manager_by_id,
	get_all_cameras_status,
	start_camera_stream_by_id,
	stop_camera_stream_by_id,
)
from .auth_deps import get_current_user

router = APIRouter(tags=["camera"])


async def mjpeg_stream_generator_for_camera(
	manager: CameraManager,
	request: Request
) -> AsyncGenerator[bytes, None]:
	black_frame_cache = None

	try:
		while True:
			if await request.is_disconnected() is True:
				break

			if manager.calibration_complete.is_set() is False:
				if black_frame_cache is None:
					black = np.zeros((480, 640, 3), dtype=np.uint8)
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
					black = np.zeros((480, 640, 3), dtype=np.uint8)
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


@router.get("", dependencies=[Depends(get_current_user)])
async def list_cameras() -> dict:
	cameras = get_all_cameras_status()

	return {"cameras": cameras}


@router.get("/{camera_id}", dependencies=[Depends(get_current_user)])
async def stream_camera(
	camera_id: str,
	request: Request,
) -> StreamingResponse:
	manager = get_camera_manager_by_id(camera_id)

	if manager is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail=f"Camera '{camera_id}' not found",
		)

	try:
		await start_camera_stream_by_id(camera_id)
	except ValueError as e:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=str(e),
		)

	async def stream_with_cleanup() -> AsyncGenerator[bytes, None]:
		try:
			async for chunk in mjpeg_stream_generator_for_camera(manager, request):
				yield chunk
		finally:
			await stop_camera_stream_by_id(camera_id)

	return StreamingResponse(
		stream_with_cleanup(),
		media_type="multipart/x-mixed-replace; boundary=frame",
	)
