from __future__ import annotations

import asyncio
from typing import Optional

import cv2
import numpy as np
from hardware.camera import Camera  # type: ignore[import-untyped]

from perpetual_os1_server.core.logging_config import get_logger
from perpetual_os1_server.settings import get_settings

logger = get_logger(__name__)

class CameraStreamManager:
	def __init__(self, camera_url: str):
		self.camera_url = camera_url
		self.camera: Optional[Camera] = None
		self.is_running = False
		self.consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		self.target_fps = 12
		self.frame_interval = 1.0 / self.target_fps

	async def start(self) -> None:
		if self.is_running is True:
			return

		logger.info("camera_stream_manager_starting", url=self.camera_url)
		self.camera = Camera(url=self.camera_url)

		loop = asyncio.get_event_loop()
		try:
			await asyncio.wait_for(
				loop.run_in_executor(None, self.camera.get_correct),
				timeout=10.0
			)
			logger.info("camera_calibration_loaded")
		except asyncio.TimeoutError:
			logger.warning("camera_calibration_timeout", reason="calibration_took_too_long")
		except FileNotFoundError as e:
			logger.warning("camera_calibration_skipped", reason="calibration_images_not_found", error=str(e))
		except Exception as e:
			logger.warning("camera_calibration_failed", error=str(e), error_type=type(e).__name__)
		finally:
			self.is_running = True
			self.capture_task = asyncio.create_task(self._capture_loop())
			logger.info("camera_stream_manager_started")

	async def stop(self) -> None:
		if self.is_running is False:
			return

		logger.info("camera_stream_manager_stopping")
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
		logger.info("camera_stream_manager_stopped")

	def increment_consumers(self) -> int:
		self.consumer_count += 1
		logger.info("camera_consumer_connected", count=self.consumer_count)
		return self.consumer_count

	def decrement_consumers(self) -> int:
		self.consumer_count = max(0, self.consumer_count - 1)
		logger.info("camera_consumer_disconnected", count=self.consumer_count)
		return self.consumer_count

	async def _capture_loop(self) -> None:
		logger.info("camera_capture_loop_started", target_fps=self.target_fps)

		while self.is_running is True:
			loop_start = asyncio.get_event_loop().time()

			try:
				if self.camera is None:
					logger.warning("camera_not_initialized")
					await asyncio.sleep(1.0)
					continue

				loop = asyncio.get_event_loop()

				def sync_capture_and_process():
					if self.camera.cap is None or not self.camera.cap.isOpened():
						self.camera.cap = cv2.VideoCapture(self.camera.url, cv2.CAP_FFMPEG)
						if not self.camera.cap.isOpened():
							return None, None, None
						self.camera.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
						self.camera.cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
						self.camera.cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)
						if not self.camera.cap_initialized:
							import time
							t0 = time.time()
							while time.time() - t0 < 4.25:
								self.camera.cap.grab()
								time.sleep(0.001)
							self.camera.cap_initialized = True

					for _ in range(3):
						self.camera.cap.grab()

					ok, frame = self.camera.cap.read()
					if not ok or frame is None:
						if self.camera.cap is not None:
							self.camera.cap.release()
							self.camera.cap = None
							self.camera.cap_initialized = False
						return None, None, None

					corrected = self.camera.correct(frame)
					bbox = self.camera.detect_white_squares(corrected)
					return frame, corrected, bbox

				frame, corrected, bbox = await loop.run_in_executor(None, sync_capture_and_process)

				if frame is None:
					logger.warning("camera_capture_failed_no_frame")
					await asyncio.sleep(1.0)
					continue

				if bbox is not None:
					annotated = self._draw_detection_overlay(corrected, bbox)
				else:
					annotated = corrected

				success, buffer = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])

				if success is True:
					async with self.frame_lock:
						self.latest_frame = buffer.tobytes()

			except RuntimeError as e:
				if "bad cam" in str(e):
					logger.error("camera_connection_failed", url=self.camera_url, error=str(e))
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

		logger.info("camera_capture_loop_stopped")

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


camera_manager: Optional[CameraStreamManager] = None


async def init_camera_manager() -> None:
	global camera_manager

	settings = get_settings()

	if settings.camera.enabled is False:
		logger.info("camera_disabled", reason="settings_configuration")
		return

	camera_manager = CameraStreamManager(settings.camera.url)
	logger.info("camera_manager_initialized")

	asyncio.create_task(camera_manager.start())
	logger.info("camera_manager_starting_in_background")


async def start_camera_stream() -> None:
	global camera_manager

	if camera_manager is None:
		raise ValueError("Camera manager not initialized")

	camera_manager.increment_consumers()


async def stop_camera_stream() -> None:
	global camera_manager

	if camera_manager is None:
		return

	consumer_count = camera_manager.decrement_consumers()


async def get_camera_manager() -> CameraStreamManager:
	global camera_manager

	if camera_manager is None:
		raise ValueError("Camera manager not initialized")

	return camera_manager
