from __future__ import annotations

import asyncio
from typing import Optional, Protocol

import cv2
import numpy as np

from openshelf_os_server.core.logging_config import get_logger

logger = get_logger(__name__)

class RawCamera(Protocol):
	def capture_raw(self) -> np.ndarray: ...


class CameraStreamManager:
	def __init__(self, camera: RawCamera, camera_type: str = "OpenCvCamera"):
		self.camera: Optional[RawCamera] = camera
		self.camera_type = camera_type
		self.is_running = False
		self.consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		self.target_fps = 12
		self.frame_interval = 1.0 / self.target_fps
		self.calibration_complete = asyncio.Event()
		self.unregistered = asyncio.Event()

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

		if self.unregistered.is_set() is True:
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

				camera = self.camera
				loop = asyncio.get_event_loop()
				frame = await loop.run_in_executor(None, camera.capture_raw)

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

	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


CameraManager = CameraStreamManager
camera_registry: dict[str, CameraManager] = {}

camera_manager: Optional[CameraStreamManager] = None

async def init_camera_manager(camera: RawCamera) -> None:
	global camera_manager, camera_registry

	camera_manager = CameraStreamManager(camera=camera)
	camera_registry["io"] = camera_manager
	logger.info("io_camera_registered")


async def init_sizer_camera_manager(camera: RawCamera) -> None:
	camera_registry["iobox_sizer"] = CameraStreamManager(camera=camera, camera_type="IOBoxSizerCamera")
	logger.info("iobox_sizer_camera_registered")


async def unregister_camera_manager(camera_id: str) -> None:
	manager = camera_registry.pop(camera_id, None)

	if manager is None:
		return

	manager.unregistered.set()
	await manager.stop()



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

	consumer_count = manager.decrement_consumers()

	if consumer_count == 0 and manager.is_running is True:
		await manager.stop()


def get_camera_manager_by_id(camera_id: str) -> Optional[CameraManager]:
	return camera_registry.get(camera_id)


def get_all_cameras_status() -> list[dict]:
	cameras = []

	for camera_id, manager in camera_registry.items():
		camera_type = getattr(manager, "camera_type", "OpenCvCamera")

		cameras.append({
			"id": camera_id,
			"type": camera_type,
			"running": manager.is_running,
			"consumers": manager.consumer_count,
			"has_frame": manager.latest_frame is not None
		})

	return cameras
