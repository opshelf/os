from __future__ import annotations

import asyncio
import multiprocessing
import queue
import time
from multiprocessing.context import BaseContext
from multiprocessing.queues import Queue
from typing import Optional, Union

import cv2
import numpy as np
from hardware.iocam import AFSCam

from perpetual_os1_server.core.logging_config import get_logger

logger = get_logger(__name__)

CameraManager = Union["CameraStreamManager", "ToolCameraStreamManager"]
camera_registry: dict[str, CameraManager] = {}


def _tool_camera_worker(
	rtsp_url: str,
	frame_queue: Queue,
	stop_event: multiprocessing.synchronize.Event,
	target_fps: int,
	max_frame_width: int,
	jpeg_quality: int,
) -> None:
	cap: Optional[cv2.VideoCapture] = None
	frame_interval = 1.0 / target_fps

	try:
		while stop_event.is_set() is False:
			loop_start = time.monotonic()

			try:
				if cap is None or cap.isOpened() is False:
					cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
					cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
					cap.set(cv2.CAP_PROP_FRAME_WIDTH, max_frame_width)
					cap.set(cv2.CAP_PROP_FPS, target_fps)

					if cap.isOpened() is False:
						time.sleep(2.0)
						continue

				source_fps = cap.get(cv2.CAP_PROP_FPS)
				frames_to_skip = 1
				if source_fps > target_fps:
					frames_to_skip = min(30, max(1, int(source_fps / target_fps) - 1))

				for _ in range(frames_to_skip):
					cap.grab()

				ret, frame = cap.read()
				if ret is False or frame is None:
					time.sleep(0.5)
					continue

				height, width = frame.shape[:2]
				if width > max_frame_width:
					target_height = max(1, int(height * (max_frame_width / width)))
					frame = cv2.resize(frame, (max_frame_width, target_height), interpolation=cv2.INTER_AREA)

				success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
				if success is True:
					frame_bytes = buffer.tobytes()
					while frame_queue.empty() is False:
						try:
							frame_queue.get_nowait()
						except queue.Empty:
							break
					try:
						frame_queue.put_nowait(frame_bytes)
					except queue.Full:
						pass

			except Exception:
				time.sleep(1.0)
				continue

			elapsed = time.monotonic() - loop_start
			time.sleep(max(0.0, frame_interval - elapsed))
	finally:
		if cap is not None:
			cap.release()

class CameraStreamManager:
	def __init__(self, camera: AFSCam):
		self.camera: Optional[AFSCam] = camera
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
		self.process: Optional[multiprocessing.Process] = None
		self.stop_event: Optional[multiprocessing.synchronize.Event] = None
		self.frame_queue: Optional[Queue] = None
		self.process_context: BaseContext = multiprocessing.get_context("spawn")
		self.target_fps = 5
		self.frame_interval = 1.0 / self.target_fps
		self.max_frame_width = 640
		self.jpeg_quality = 65
		self.calibration_complete = asyncio.Event()

	async def start(self) -> None:
		if self.is_running is True:
			return

		self.calibration_complete.set()
		self.is_running = True
		self._start_worker()
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

		self._stop_worker()
		self.latest_frame = None

	def increment_consumers(self) -> int:
		self.consumer_count += 1
		return self.consumer_count

	def decrement_consumers(self) -> int:
		self.consumer_count = max(0, self.consumer_count - 1)
		return self.consumer_count

	def _start_worker(self) -> None:
		self._stop_worker()

		self.frame_queue = self.process_context.Queue(maxsize=2)
		self.stop_event = self.process_context.Event()
		self.process = self.process_context.Process(
			target=_tool_camera_worker,
			args=(
				self.rtsp_url,
				self.frame_queue,
				self.stop_event,
				self.target_fps,
				self.max_frame_width,
				self.jpeg_quality,
			),
			daemon=True,
		)
		self.process.start()
		logger.info("tool_camera_worker_started", rtsp_url=self.rtsp_url, pid=self.process.pid)

	def _stop_worker(self) -> None:
		if self.stop_event is not None:
			self.stop_event.set()

		if self.process is not None:
			self.process.join(timeout=2.0)
			if self.process.is_alive() is True:
				self.process.terminate()
				self.process.join(timeout=2.0)
			if self.process.is_alive() is True:
				self.process.kill()
				self.process.join(timeout=1.0)

		self.process = None
		self.stop_event = None
		self.frame_queue = None

	def _drain_frame_queue(self) -> Optional[bytes]:
		if self.frame_queue is None:
			return None

		latest_frame = None

		while True:
			try:
				latest_frame = self.frame_queue.get_nowait()
			except queue.Empty:
				return latest_frame

	async def _capture_loop(self) -> None:
		restart_delay = 1.0

		while self.is_running is True:
			if self.process is None or self.process.is_alive() is False:
				exitcode = None if self.process is None else self.process.exitcode
				logger.error(
					"tool_camera_worker_exited",
					rtsp_url=self.rtsp_url,
					exitcode=exitcode,
					restart_delay=restart_delay,
				)
				await asyncio.sleep(restart_delay)
				if self.is_running is False:
					return
				self._start_worker()
				restart_delay = min(30.0, restart_delay * 2.0)
				continue

			frame = self._drain_frame_queue()
			if frame is not None:
				restart_delay = 1.0
				async with self.frame_lock:
					self.latest_frame = frame

			await asyncio.sleep(self.frame_interval)
	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


camera_manager: Optional[CameraStreamManager] = None
tool_camera_manager: Optional[ToolCameraStreamManager] = None
overhead_camera_manager: Optional[ToolCameraStreamManager] = None

async def init_camera_manager(camera: AFSCam) -> None:
	global camera_manager, camera_registry

	camera_manager = CameraStreamManager(camera=camera)
	camera_registry["io"] = camera_manager
	logger.info("io_camera_registered")


async def init_tool_camera_manager(rtsp_url: str) -> None:
	global tool_camera_manager, camera_registry

	if rtsp_url is None or rtsp_url == "":
		logger.warning("tool_camera_not_configured", reason="no_rtsp_url")
		return

	tool_camera_manager = ToolCameraStreamManager(rtsp_url=rtsp_url)
	camera_registry["tool"] = tool_camera_manager
	logger.info("tool_camera_registered", rtsp_url=rtsp_url)


async def init_overhead_camera_manager(rtsp_url: str) -> None:
	global overhead_camera_manager, camera_registry

	if rtsp_url is None or rtsp_url == "":
		logger.warning("overhead_camera_not_configured", reason="no_rtsp_url")
		return

	overhead_camera_manager = ToolCameraStreamManager(rtsp_url=rtsp_url)
	camera_registry["overhead"] = overhead_camera_manager
	logger.info("overhead_camera_registered", rtsp_url=rtsp_url)


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

	if consumer_count == 0 and camera_manager.is_running is True:
		await camera_manager.stop()


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
			camera_type = "IOCamera"
		elif camera_id == "overhead":
			camera_type = "OverheadCamera"
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
