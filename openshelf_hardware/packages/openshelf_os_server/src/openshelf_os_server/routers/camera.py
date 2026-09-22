from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

import base64

import cv2
import numpy as np

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from openshelf_os_server.documentation import BooleanSuccess, response_docs, stream_docs
from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.core.camera import (
	CameraManager,
	ToolCameraStreamManager,
	camera_registry,
	get_all_cameras_status,
	get_camera_manager_by_id,
	start_camera_stream_by_id,
	start_nalu_stream_by_id,
	stop_camera_stream_by_id,
	stop_nalu_stream_by_id,
)
from openshelf_os_server.core.camera_recording import get_recording_store
from openshelf_os_server.core.logging_config import get_logger
from openshelf_os_server.database.models import User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_settings
from .auth_deps import get_current_user

logger = get_logger(__name__)

# Mean-abs-diff cutoff on a 32x32 grayscale thumbnail. A static cabinet
# re-encoded at q65-70 sits near zero; robot motion lifts it far past this.
MJPEG_MOTION_DIFF_THRESHOLD = 2.5
MJPEG_HEARTBEAT_SECONDS = 1.0


class CameraInfo(BaseModel):
    id: str
    type: str
    online: bool
    running: bool
    consumers: int
    has_frame: bool


class CamerasResponse(BaseModel):
    cameras: list[CameraInfo]


class RecordStartRequest(BaseModel):
    source: Optional[str] = Field(
        default="auto",
        description="auto (recording stream when configured, else view), view, or record",
    )
    max_duration_s: Optional[int] = Field(default=None, gt=0, le=3600)


class RecordStatusResponse(BaseModel):
    is_recording: bool
    source: Optional[str] = None
    file_name: Optional[str] = None
    started_at: Optional[str] = None
    elapsed_s: Optional[float] = None
    size_bytes: Optional[int] = None
    max_duration_s: Optional[int] = None
    dropped_units: int = 0
    written_packets: int = 0


class RecordingFile(BaseModel):
    camera_id: str
    file_name: str
    size_bytes: int
    modified_at: str
    duration_s: Optional[float] = None
    is_recording: bool = False


class RecordingsResponse(BaseModel):
    recordings: list[RecordingFile]


router = APIRouter(tags=["camera"])


def _get_recording_manager(camera_id: str) -> CameraManager:
    manager = get_camera_manager_by_id(camera_id)

    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Camera '{camera_id}' not found",
        )

    return manager


def _resolve_recording_source(manager: CameraManager, requested: str) -> str:
    if requested not in ("auto", "view", "record"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid source: {requested}",
        )

    if requested == "auto":
        return (
            "record"
            if isinstance(manager, ToolCameraStreamManager) and manager.recording_url != ""
            else "view"
        )

    return requested


def _mjpeg_frame_thumbnail(frame_bytes: bytes) -> Optional[np.ndarray]:
	decoded = cv2.imdecode(np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)

	if decoded is None:
		return None

	return cv2.resize(decoded, (32, 32), interpolation=cv2.INTER_AREA)


async def mjpeg_stream_generator_for_camera(
	manager: CameraManager,
	request: Request
) -> AsyncGenerator[bytes, None]:
	seen_frame: Optional[bytes] = None
	sent_thumbnail: Optional[np.ndarray] = None
	last_sent_at = 0.0

	try:
		while True:
			if await request.is_disconnected() is True:
				break

			if manager.unregistered.is_set() is True:
				break

			if manager.mjpeg_path_stale() is True:
				logger.warning("camera_stream_stalled", stale_after_s=manager.stale_frame_timeout)
				break

			frame_bytes = await manager.get_latest_frame()

			if frame_bytes is None:
				await asyncio.sleep(0.05)
				continue

			# The capture loop replaces latest_frame with a new object for every
			# frame, so object identity marks "a new frame arrived". Every
			# continue path below must await, or the loop spins without
			# yielding to the event loop.
			if frame_bytes is seen_frame:
				await asyncio.sleep(0.05)
				continue

			seen_frame = frame_bytes
			now = asyncio.get_event_loop().time()
			thumbnail = _mjpeg_frame_thumbnail(frame_bytes)

			# Diff-gate: on a static scene re-send only at the heartbeat rate.
			# The reference always tracks the last SENT frame, so it re-anchors
			# after any lasting scene change (placed item, lighting drift).
			if (
				thumbnail is not None
				and sent_thumbnail is not None
				and (now - last_sent_at) < MJPEG_HEARTBEAT_SECONDS
			):
				diff = cv2.absdiff(sent_thumbnail, thumbnail)
				if float(diff.mean()) < MJPEG_MOTION_DIFF_THRESHOLD:
					await asyncio.sleep(1.0 / manager.target_fps)
					continue

			yield (
				b"--frame\r\n"
				b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
			)

			if thumbnail is not None:
				sent_thumbnail = thumbnail
			last_sent_at = now

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
    "/recordings",
    dependencies=[Depends(get_current_user)],
    summary="List recorded camera files",
    responses=response_docs(model=RecordingsResponse, errors=(401,)),
)
async def list_recordings() -> RecordingsResponse:
	"""List recorded camera files.

	Returns every recorded file on disk with its size and finalised duration; the
	file of an in-progress recording is listed with `is_recording` true. Requires a
	user token.
	"""
	active: dict[str, dict] = {}

	for camera_id, manager in camera_registry.items():
		rec_status = manager.recording_status()
		if rec_status is not None and rec_status.get("is_recording") is True:
			active[camera_id] = rec_status

	files = get_recording_store().list_recordings(active)

	return RecordingsResponse(recordings=[RecordingFile(**file) for file in files])


@router.get(
    "/recordings/{camera_id}/{file_name}/download",
    dependencies=[Depends(get_current_user)],
    summary="Download a recorded camera file",
    responses=response_docs(
        errors=(400, 401, 404),
        existing=stream_docs(
            "video/mp4",
            "The recorded MP4 file bytes.",
        ),
    ),
    response_class=Response,
)
async def download_recording(camera_id: str, file_name: str) -> FileResponse:
	"""Download a recorded camera file.

	Streams the MP4 from disk. Invalid names return `400`; missing files return
	`404`. Requires a user token.
	"""
	store = get_recording_store()

	if store.validate_camera_id(camera_id) is False or store.validate_file_name(file_name) is False:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Invalid recording path",
		)

	path = store.resolve(camera_id, file_name)

	if path.exists() is False:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Recording file not found",
		)

	return FileResponse(path, media_type="video/mp4", filename=file_name)


@router.delete(
    "/recordings/{camera_id}/{file_name}",
    response_model=BooleanSuccess,
    dependencies=[Depends(get_current_user)],
    summary="Delete a recorded camera file",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 404, 409)),
)
async def delete_recording(camera_id: str, file_name: str) -> BooleanSuccess:
	"""Delete a recorded camera file.

	Removes the file from disk. In-progress files return `409`; invalid names
	return `400`; missing files return `404`. Requires a user token.
	"""
	store = get_recording_store()

	if store.validate_camera_id(camera_id) is False or store.validate_file_name(file_name) is False:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Invalid recording path",
		)

	path = store.resolve(camera_id, file_name)

	if path.exists() is False:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Recording file not found",
		)

	manager = camera_registry.get(camera_id)

	if manager is not None:
		rec_status = manager.recording_status()
		if (
			rec_status is not None
			and rec_status.get("is_recording") is True
			and rec_status.get("file_name") == file_name
		):
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="Recording is still in progress",
			)

	path.unlink()

	return BooleanSuccess(success=True)


@router.post(
    "/{camera_id}/record",
    response_model=RecordStatusResponse,
    dependencies=[Depends(get_current_user)],
    summary="Start recording a camera to disk",
    responses=response_docs(errors=(400, 401, 404, 409)),
)
async def start_recording(
    camera_id: str, payload: RecordStartRequest = RecordStartRequest()
) -> RecordStatusResponse:
	"""Start recording a camera to disk.

	Records the camera at full stream quality into an MP4 on disk. `source` selects
	the recording stream (the higher-resolution one when configured) or the live
	view; an already recording camera returns `409`. Requires a user token.
	"""
	manager = _get_recording_manager(camera_id)
	source = _resolve_recording_source(manager, payload.source or "auto")

	try:
		await start_camera_stream_by_id(camera_id, background=True)
	except ValueError as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
		) from exc

	try:
		await manager.start_recording(camera_id, source, payload.max_duration_s)
	except ValueError as exc:
		await stop_camera_stream_by_id(camera_id, background=True)
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT, detail=str(exc)
		) from exc

	rec_status = manager.recording_status()

	if rec_status is None:
		raise HTTPException(
			status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
			detail="Recording failed to start",
		)

	return RecordStatusResponse(**rec_status)


@router.get(
    "/{camera_id}/record",
    response_model=RecordStatusResponse,
    dependencies=[Depends(get_current_user)],
    summary="Read camera recording status",
    responses=response_docs(errors=(401, 404)),
)
async def get_recording_status(camera_id: str) -> RecordStatusResponse:
	"""Read camera recording status.

	Reports whether the camera is recording, the source, elapsed time, and file
	size. A finalised recording reports `is_recording` false. Requires a user
	token.
	"""
	manager = _get_recording_manager(camera_id)
	rec_status = manager.recording_status()

	if rec_status is None:
		return RecordStatusResponse(is_recording=False)

	return RecordStatusResponse(**rec_status)


@router.delete(
    "/{camera_id}/record",
    response_model=RecordStatusResponse,
    dependencies=[Depends(get_current_user)],
    summary="Stop the camera recording",
    responses=response_docs(errors=(401, 404, 409)),
)
async def stop_recording_endpoint(camera_id: str) -> RecordStatusResponse:
	"""Stop the camera recording.

	Finalises the MP4 on disk and releases the camera. A camera that is not
	recording returns `409`; the response carries the final file status. Requires a
	user token.
	"""
	manager = _get_recording_manager(camera_id)

	if manager.recording_status() is None:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail="Camera is not recording",
		)

	rec_status = manager.recording_status()

	try:
		await manager.stop_recording()
	finally:
		await stop_camera_stream_by_id(camera_id, background=True)

	if rec_status is not None:
		return RecordStatusResponse(**{**rec_status, "is_recording": False})

	return RecordStatusResponse(is_recording=False)


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

async def _websocket_current_user(websocket: WebSocket) -> Optional[User]:
	"""Token from the query string or the Authorization header, mirroring the
	dependency-injected path for image/streaming clients."""
	settings = get_settings()
	token = websocket.query_params.get("token")

	if token is None:
		authorization = websocket.headers.get("Authorization", "")
		scheme, separator, header_token = authorization.partition(" ")
		if separator == " " and scheme.lower() == "bearer":
			token = header_token

	async with AsyncSessionLocal() as db:
		if token is not None:
			auth_service = AuthService(
				settings.auth.jwt_secret or "",
				settings.auth.jwt_algorithm,
			)
			payload = auth_service.decode_token(token)
			if payload is not None and payload.get("type") == "access":
				result = await db.execute(
					select(User)
					.options(selectinload(User.role), selectinload(User.access_level))
					.filter_by(id=payload.get("user_id"), is_active=True)
				)
				user = result.scalar_one_or_none()
				if user is not None:
					return user

		if settings.auth.dev_mode and settings.auth.dev_auto_user:
			result = await db.execute(
				select(User)
				.options(selectinload(User.role), selectinload(User.access_level))
				.filter_by(email=settings.auth.dev_auto_user, is_active=True)
			)
			user = result.scalar_one_or_none()
			if user is not None:
				return user

	return None


async def _wait_for_nalu_header(manager: ToolCameraStreamManager, timeout: float) -> Optional[dict]:
	deadline = asyncio.get_event_loop().time() + timeout

	while asyncio.get_event_loop().time() < deadline:
		meta = manager.nalu_codec_meta()
		fresh = manager.nalu_last_frame_at is not None and (asyncio.get_event_loop().time() - manager.nalu_last_frame_at) <= manager.stale_frame_timeout
		if meta is not None and fresh is True:
			return {
				"type": "header",
				"codec": meta.codec,
				"description": base64.b64encode(meta.description).decode("ascii"),
				"width": meta.width,
				"height": meta.height,
				"fps": meta.fps,
			}

		await asyncio.sleep(0.05)

	return None


@router.websocket("/ws/{camera_id}")
async def stream_camera_h264(websocket: WebSocket, camera_id: str) -> None:
	"""Live H.264 stream for WebCodecs clients.

	The first message is a JSON header with the avc1 codec string, the avcC
	description (base64), and stream dimensions. Every later message is binary:
	a run of access units, each length-prefixed with a 4-byte big-endian
	usize. The camera's last ~2 s of access units are replayed before the
	live tail so the decoder can start immediately. Unknown or MJPEG-only
	cameras close with `4404`; unreachable cameras close with `4503`.
	"""
	await websocket.accept()
	user = await _websocket_current_user(websocket)

	if user is None:
		logger.info("h264_stream_unauthenticated", camera_id=camera_id)
		await websocket.close(code=4401)
		return

	manager = get_camera_manager_by_id(camera_id)

	if not isinstance(manager, ToolCameraStreamManager):
		await websocket.close(code=4404)
		return

	try:
		await start_nalu_stream_by_id(camera_id)
	except Exception as e:
		logger.warning("h264_stream_start_failed", camera_id=camera_id, error=str(e))
		await websocket.close(code=4503)
		return

	try:
		header = await _wait_for_nalu_header(manager, manager.first_frame_timeout)
		if header is None:
			logger.warning(
				"h264_stream_unreachable",
				camera_id=camera_id,
				timeout_s=manager.first_frame_timeout,
			)
			await websocket.close(code=4503)
			return

		await websocket.send_json(header)
		subscriber = manager.add_nalu_subscriber()
		disconnect_event = asyncio.Event()

		async def _watch_disconnect() -> None:
			# A client close is only observable through receive(), which
			# returns the queued disconnect message and marks the socket
			# disconnected. Park one read for the connection's lifetime.
			try:
				message = await websocket.receive()
			except Exception:
				# The socket is gone (ASGI error or early close); treat it as
				# a disconnect.
				message = {"type": "websocket.disconnect"}
			if message.get("type") == "websocket.disconnect":
				disconnect_event.set()

		disconnect_watcher = asyncio.create_task(_watch_disconnect())
		try:
			while True:
				try:
					message = await asyncio.wait_for(subscriber.get(), timeout=1.0)
				except asyncio.TimeoutError:
					# No AUs within the tick. A client close never raises here,
					# so end the stream when the watcher sees it, and with 4503
					# when the view goes stale.
					if disconnect_event.is_set() is True:
						break
					if (
						manager.nalu_last_frame_at is None
						or (asyncio.get_event_loop().time() - manager.nalu_last_frame_at)
						> manager.stale_frame_timeout
				):
						logger.warning(
							"h264_stream_stalled",
							camera_id=camera_id,
							stale_after_s=manager.stale_frame_timeout,
						)
						await websocket.close(code=4503)
						break
					continue
				if message is None:
					break
				await websocket.send_bytes(message)
		finally:
			disconnect_watcher.cancel()
			try:
				await disconnect_watcher
			except (asyncio.CancelledError, Exception):
				pass
			manager.remove_nalu_subscriber(subscriber)
	except WebSocketDisconnect:
		pass
	except Exception as e:
		logger.warning(
			"h264_stream_error",
			camera_id=camera_id,
			error=str(e),
			error_type=type(e).__name__,
		)
	finally:
		await stop_nalu_stream_by_id(camera_id)
