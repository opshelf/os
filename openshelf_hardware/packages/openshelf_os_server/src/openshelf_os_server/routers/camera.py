from __future__ import annotations
from openshelf_os_server.documentation import response_docs, stream_docs
from fastapi.responses import Response

import asyncio
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from openshelf_os_server.core.camera import (
	CameraManager,
	get_camera_manager_by_id,
	get_all_cameras_status,
	start_camera_stream_by_id,
	stop_camera_stream_by_id,
)
from openshelf_os_server.core.logging_config import get_logger
from .auth_deps import get_current_user

logger = get_logger(__name__)


class CameraInfo(BaseModel):
    id: str
    type: str
    online: bool
    running: bool
    consumers: int
    has_frame: bool


class CamerasResponse(BaseModel):
    cameras: list[CameraInfo]


router = APIRouter(tags=["camera"])


async def mjpeg_stream_generator_for_camera(
	manager: CameraManager,
	request: Request
) -> AsyncGenerator[bytes, None]:
	try:
		while True:
			if await request.is_disconnected() is True:
				break

			if manager.unregistered.is_set() is True:
				break

			if manager.is_frame_stale() is True:
				logger.warning("camera_stream_stalled", stale_after_s=manager.stale_frame_timeout)
				break

			frame_bytes = await manager.get_latest_frame()

			if frame_bytes is None:
				await asyncio.sleep(0.05)
				continue

			yield (
				b"--frame\r\n"
				b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
			)

			await asyncio.sleep(1.0 / manager.target_fps)

	except asyncio.CancelledError:
		pass


@router.get(
    "",
    dependencies=[Depends(get_current_user)],
    summary="List available cameras",
    responses=response_docs(model=CamerasResponse, errors=(401,)),
)
async def list_cameras() -> dict:
	"""List available cameras.

	Returns configured camera IDs and status for selecting a stream. Requires a user token.
	Use a returned ID with the camera streaming endpoint.
	"""
	cameras = await get_all_cameras_status()

	return {"cameras": cameras}


@router.get(
    "/{camera_id}",
    dependencies=[Depends(get_current_user)],
    summary="Stream camera frames",
    responses=response_docs(
        errors=(401, 404, 503),
        existing=stream_docs(
            "multipart/x-mixed-replace; boundary=frame",
            "Multipart JPEG frames; each part has Content-Type: image/jpeg.",
            None,
        ),
    ),
    response_class=Response,
)
async def stream_camera(
	camera_id: str,
	request: Request,
) -> StreamingResponse:
	"""Stream camera frames.

	Returns an MJPEG stream (`multipart/x-mixed-replace; boundary=frame`) with JPEG frames.
	Starts the selected stream and releases it on disconnect. Unknown IDs return `404`; no
	available frames or camera failures return `503`. A user token may be sent through
	`?token=` for image clients.
	"""
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

	try:
		is_delivering_frames = await manager.wait_for_fresh_frame(manager.first_frame_timeout)
	except BaseException:
		await stop_camera_stream_by_id(camera_id)
		raise

	if is_delivering_frames is False:
		await stop_camera_stream_by_id(camera_id)
		logger.warning(
			"camera_unreachable",
			camera_id=camera_id,
			timeout_s=manager.first_frame_timeout,
		)
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail=f"Camera '{camera_id}' is not delivering frames",
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
