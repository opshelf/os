from __future__ import annotations

import asyncio
from typing import Optional, Union

import cv2
import numpy as np
from hardware.iocam import AFSCam

from perpetual_os1_server.core.logging_config import get_logger

logger = get_logger(__name__)

CameraManager = Union["CameraStreamManager", "ToolCameraStreamManager"]
camera_registry: dict[str, CameraManager] = {}

class CameraStreamManager:
	def __init__(self, camera: AFSCam, depth_enabled: bool = False):
		self.camera: Optional[AFSCam] = camera
		self.depth_enabled = depth_enabled
		self.is_running = False
		self.consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		self.target_fps = 12
		self.frame_interval = 1.0 / self.target_fps
		self.calibration_complete = asyncio.Event()

	# run and then pull from this, or size separately
	async def start(self) -> None:
		if self.is_running is True:
			return

		self.calibration_complete.set()
		self.is_running = True
		self.capture_task = asyncio.create_task(self._capture_loop())
		logger.info("camera_ready")

	async def stop(self) -> None:
		if self.is_running is False:
			return

		self.is_running = False

		if self.capture_task is not None:
			self.capture_task.cancel()
			try:
				await self.capture_task
			except asyncio.CancelledError:
				pass
			self.capture_task = None

		self.camera = None
		self.latest_frame = None

	def increment_consumers(self) -> int:
		self.consumer_count += 1
		return self.consumer_count

	def decrement_consumers(self) -> int:
		self.consumer_count = max(0, self.consumer_count - 1)
		return self.consumer_count

	async def _capture_loop(self) -> None:
		while self.is_running is True:
			loop_start = asyncio.get_event_loop().time()

			try:
				if self.camera is None:
					await asyncio.sleep(1.0)
					continue

				loop = asyncio.get_event_loop()

				if self.depth_enabled is True:
					def sync_capture_and_process() -> np.ndarray | None:
						frame_agg = self.camera.measure_sync(n_frames=10)
						draw: np.ndarray | None = frame_agg.get("annotated_frame")
						return draw

					frame = await loop.run_in_executor(None, sync_capture_and_process)
				else:
					frame = await loop.run_in_executor(None, self.camera.capture_raw)

				if frame is None:
					await asyncio.sleep(1.0)
					continue

				success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

				if success is True:
					async with self.frame_lock:
						self.latest_frame = buffer.tobytes()

			except RuntimeError as e:
				if "bad cam" in str(e):
					logger.error("camera_connection_failed", error=str(e))
					await asyncio.sleep(5.0)
				else:
					logger.error("camera_runtime_error", error=str(e), exc_info=True)
					await asyncio.sleep(1.0)
				continue
			except Exception as e:
				logger.error("camera_frame_processing_error", error=str(e), error_type=type(e).__name__, exc_info=True)
				await asyncio.sleep(1.0)
				continue

			elapsed = asyncio.get_event_loop().time() - loop_start
			sleep_time = max(0.0, self.frame_interval - elapsed)
			await asyncio.sleep(sleep_time)

	def _draw_detection_overlay(self, frame: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
		annotated = frame.copy()
		x, y, w, h = bbox
		cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 255, 0), 2)

		cx = x + w // 2
		cy = y + h // 2
		cv2.circle(annotated, (cx, cy), 5, (0, 255, 0), -1)

		return annotated

	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


class ToolCameraStreamManager:
	def __init__(self, rtsp_url: str):
		self.rtsp_url = rtsp_url
		self.is_running = False
		self.consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		self.target_fps = 10
		self.frame_interval = 1.0 / self.target_fps
		self.calibration_complete = asyncio.Event()

	async def start(self) -> None:
		if self.is_running is True:
			return

		self.calibration_complete.set()
		self.is_running = True
		self.capture_task = asyncio.create_task(self._capture_loop())
		logger.info("tool_camera_ready", rtsp_url=self.rtsp_url)

	async def stop(self) -> None:
		if self.is_running is False:
			return

		self.is_running = False

		if self.capture_task is not None:
			self.capture_task.cancel()
			try:
				await self.capture_task
			except asyncio.CancelledError:
				pass
			self.capture_task = None

		self.latest_frame = None

	def increment_consumers(self) -> int:
		self.consumer_count += 1
		return self.consumer_count

	def decrement_consumers(self) -> int:
		self.consumer_count = max(0, self.consumer_count - 1)
		return self.consumer_count

	async def _capture_loop(self) -> None:
		cap: Optional[cv2.VideoCapture] = None

		try:
			while self.is_running is True:
				loop_start = asyncio.get_event_loop().time()

				try:
					if cap is None or cap.isOpened() is False:
						cap = cv2.VideoCapture(self.rtsp_url)
						if cap.isOpened() is False:
							logger.warning("tool_camera_connection_failed", rtsp_url=self.rtsp_url)
							await asyncio.sleep(2.0)
							continue

					loop = asyncio.get_event_loop()

					def sync_capture() -> Optional[np.ndarray]:
						if cap is None:
							return None
						ret, frame = cap.read()
						if ret is True and frame is not None:
							return frame
						return None

					frame = await loop.run_in_executor(None, sync_capture)

					if frame is None:
						await asyncio.sleep(0.5)
						continue

					success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])

					if success is True:
						async with self.frame_lock:
							self.latest_frame = buffer.tobytes()

				except Exception as e:
					logger.error("tool_camera_error", error=str(e), error_type=type(e).__name__, exc_info=True)
					await asyncio.sleep(1.0)
					continue

				elapsed = asyncio.get_event_loop().time() - loop_start
				sleep_time = max(0.0, self.frame_interval - elapsed)
				await asyncio.sleep(sleep_time)

		finally:
			if cap is not None:
				cap.release()

	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


camera_manager: Optional[CameraStreamManager] = None
tool_camera_manager: Optional[ToolCameraStreamManager] = None

async def init_camera_manager(camera: AFSCam, depth_enabled: bool = False) -> None:
	global camera_manager, camera_registry

	camera_manager = CameraStreamManager(camera=camera, depth_enabled=depth_enabled)
	camera_registry["io"] = camera_manager
	logger.info("io_camera_registered", depth_enabled=depth_enabled)


async def init_tool_camera_manager(rtsp_url: str) -> None:
	global tool_camera_manager, camera_registry

	if rtsp_url is None or rtsp_url == "":
		logger.warning("tool_camera_not_configured", reason="no_rtsp_url")
		return

	tool_camera_manager = ToolCameraStreamManager(rtsp_url=rtsp_url)
	camera_registry["tool"] = tool_camera_manager
	logger.info("tool_camera_registered", rtsp_url=rtsp_url)


async def start_camera_stream() -> None:
	global camera_manager

	if camera_manager is None:
		raise ValueError("Camera manager not initialized")

	camera_manager.increment_consumers()


async def stop_camera_stream() -> None:
	global camera_manager

	if camera_manager is None:
		return

	camera_manager.decrement_consumers()


async def start_camera_stream_by_id(camera_id: str) -> None:
	manager = camera_registry.get(camera_id)

	if manager is None:
		raise ValueError(f"Camera '{camera_id}' not found")

	if manager.is_running is False:
		await manager.start()

	manager.increment_consumers()


async def stop_camera_stream_by_id(camera_id: str) -> None:
	manager = camera_registry.get(camera_id)

	if manager is None:
		return

	manager.decrement_consumers()


async def get_camera_manager() -> CameraStreamManager:
	global camera_manager

	if camera_manager is None:
		raise ValueError("Camera manager not initialized")

	return camera_manager


def get_camera_manager_by_id(camera_id: str) -> Optional[CameraManager]:
	return camera_registry.get(camera_id)


def get_all_camera_managers() -> dict[str, CameraManager]:
	return camera_registry


def get_all_cameras_status() -> list[dict]:
	cameras = []

	for camera_id, manager in camera_registry.items():
		if isinstance(manager, CameraStreamManager):
			camera_type = "IOCamera (depth)" if manager.depth_enabled is True else "IOCamera"
		else:
			camera_type = "ToolCamera"

		cameras.append({
			"id": camera_id,
			"type": camera_type,
			"running": manager.is_running,
			"consumers": manager.consumer_count,
			"has_frame": manager.latest_frame is not None
		})

	return cameras
