from __future__ import annotations

import asyncio
from typing import Optional

import cv2
import numpy as np
from hardware.iocam import AFSCam

# from perpetual_os1_server.core.logging_config import get_logger
# from perpetual_os1_server.settings import get_settings

# logger = get_logger(__name__)

class CameraStreamManager:
	def __init__(self, camera: AFSCam):
		self.camera = camera
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

		# logger.info("camera_starting")
		# self.camera = cv2.VideoCapture(self._camera_index, cv2.CAP_V4L2)

		loop = asyncio.get_event_loop()
		# try:
		# 	await asyncio.wait_for(
		# 		loop.run_in_executor(None, self.camera.get_correct),
		# 		timeout=30.0
		# 	)
		# 	logger.info("camera_calibration_complete")
		# except asyncio.TimeoutError:
		# 	logger.warning("camera_calibration_timeout")
		# except FileNotFoundError:
		# 	logger.warning("camera_calibration_skipped")
		# except Exception as e:
		# 	logger.warning("camera_calibration_failed", error=str(e))

		self.calibration_complete.set()
		self.is_running = True
		self.capture_task = asyncio.create_task(self._capture_loop())
		# logger.info("camera_ready")

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

				def sync_capture_and_process():
					frame_agg = self.camera.measure_sync(n_frames=10)
					draw = frame_agg.get("last_draw")
					return draw

				frame = await loop.run_in_executor(None, sync_capture_and_process)

				if frame is None:
					await asyncio.sleep(1.0)
					continue

				# if bbox is not None:
				# 	annotated = self._draw_detection_overlay(corrected, bbox)
				# else:
				# 	annotated = corrected

				# success, buffer = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
				success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

				if success is True:
					async with self.frame_lock:
						self.latest_frame = buffer.tobytes()

			except RuntimeError as e:
				if "bad cam" in str(e):
					print("connection failed")
					# logger.error("camera_connection_failed", url=self.camera_url, error=str(e))
					await asyncio.sleep(5.0)
				else:
					print("runtime error")
					# logger.error("camera_runtime_error", error=str(e), exc_info=True)
					await asyncio.sleep(1.0)
				continue
			except Exception as e:
				print("frame processing error")
				# logger.error("camera_frame_processing_error", error=str(e), error_type=type(e).__name__, exc_info=True)
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

camera_manager: Optional[CameraStreamManager] = None

async def init_camera_manager(camera) -> None:
	global camera_manager

	# settings = get_settings()

	# if settings.camera.enabled is False:
	# 	return

	camera_manager = CameraStreamManager(camera=camera)
	asyncio.create_task(camera_manager.start())


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
