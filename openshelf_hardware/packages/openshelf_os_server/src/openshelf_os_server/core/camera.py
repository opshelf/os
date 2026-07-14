from __future__ import annotations

import asyncio
import multiprocessing
import queue
import time
from multiprocessing.context import BaseContext
from multiprocessing.queues import Queue
from typing import Any, Optional, Protocol, Union, cast
from urllib.parse import urlparse

import cv2
import numpy as np

from openshelf_os_server.core.init_progress import get_init_tracker
from openshelf_os_server.core.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_RTSP_PORT = 554
RTSP_PROBE_TIMEOUT = 2.0

class RawCamera(Protocol):
	def capture_raw(self) -> np.ndarray: ...


CameraManager = Union["CameraStreamManager", "ToolCameraStreamManager"]
camera_registry: dict[str, CameraManager] = {}


async def is_rtsp_url_reachable(rtsp_url: str, timeout: float = RTSP_PROBE_TIMEOUT) -> bool:
	parsed = urlparse(rtsp_url)
	host = parsed.hostname

	if host is None or host == "":
		return False

	port = parsed.port if parsed.port is not None else DEFAULT_RTSP_PORT

	try:
		_, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
	except (OSError, asyncio.TimeoutError):
		return False

	writer.close()

	try:
		await writer.wait_closed()
	except OSError:
		pass

	return True


async def _wait_for_fresh_frame(manager: "CameraManager", timeout: float) -> bool:
	deadline = time.monotonic() + timeout

	while time.monotonic() < deadline:
		if manager.is_frame_stale() is False:
			return True

		await asyncio.sleep(0.1)

	return False


def _get_tool_camera_frames_to_skip(source_fps: float, target_fps: int) -> int:
	if source_fps > target_fps:
		return min(30, max(1, int(source_fps / target_fps) - 1))

	return 1


def _prepare_tool_camera_frame(frame: np.ndarray, max_frame_width: int) -> np.ndarray:
	height, width = frame.shape[:2]
	if width <= max_frame_width:
		return frame

	target_height = max(1, int(height * (max_frame_width / width)))
	return cv2.resize(frame, (max_frame_width, target_height), interpolation=cv2.INTER_AREA)

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
				frames_to_skip = _get_tool_camera_frames_to_skip(source_fps, target_fps)

				for _ in range(frames_to_skip):
					cap.grab()

				ret, frame = cap.read()
				if ret is False or frame is None:
					time.sleep(0.5)
					continue

				frame = _prepare_tool_camera_frame(frame, max_frame_width)

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
	def __init__(self, camera: RawCamera, camera_type: str = "OpenCvCamera"):
		self.camera: Optional[RawCamera] = camera
		self.camera_type = camera_type
		self.is_running = False
		self.consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.last_frame_at: Optional[float] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		self.target_fps = 12
		self.frame_interval = 1.0 / self.target_fps
		self.first_frame_timeout = 5.0
		self.stale_frame_timeout = 5.0
		self.lifecycle_lock = asyncio.Lock()
		self.unregistered = asyncio.Event()

	# run and then pull from this, or size separately
	async def start(self) -> None:
		async with self.lifecycle_lock:
			if self.is_running is True:
				return

			self.latest_frame = None
			self.last_frame_at = None
			self.is_running = True
			self.capture_task = asyncio.create_task(self._capture_loop())
			logger.info("camera_ready")

	async def stop(self) -> None:
		async with self.lifecycle_lock:
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
			self.last_frame_at = None

	def is_frame_stale(self) -> bool:
		if self.last_frame_at is None:
			return True

		return (time.monotonic() - self.last_frame_at) > self.stale_frame_timeout

	async def wait_for_fresh_frame(self, timeout: float) -> bool:
		return await _wait_for_fresh_frame(self, timeout)

	async def probe(self) -> bool:
		return self.camera is not None

	async def is_available(self) -> bool:
		if self.is_running is True and self.is_frame_stale() is False:
			return True

		return await self.probe()

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
						self.last_frame_at = time.monotonic()

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


class ToolCameraStreamManager:
	def __init__(self, rtsp_url: str):
		self.rtsp_url = rtsp_url
		self.is_running = False
		self.consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.last_frame_at: Optional[float] = None
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
		self.first_frame_timeout = 15.0
		self.stale_frame_timeout = 10.0
		self.lifecycle_lock = asyncio.Lock()
		self.unregistered = asyncio.Event()

	async def start(self) -> None:
		async with self.lifecycle_lock:
			if self.is_running is True:
				return

			self.latest_frame = None
			self.last_frame_at = None
			self.is_running = True
			self._start_worker()
			self.capture_task = asyncio.create_task(self._capture_loop())
			logger.info("tool_camera_ready", rtsp_url=self.rtsp_url)

	async def stop(self) -> None:
		async with self.lifecycle_lock:
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
			self.last_frame_at = None

	def is_frame_stale(self) -> bool:
		if self.last_frame_at is None:
			return True

		return (time.monotonic() - self.last_frame_at) > self.stale_frame_timeout

	async def wait_for_fresh_frame(self, timeout: float) -> bool:
		return await _wait_for_fresh_frame(self, timeout)

	async def probe(self) -> bool:
		return await is_rtsp_url_reachable(self.rtsp_url)

	async def is_available(self) -> bool:
		if self.is_running is True and self.is_frame_stale() is False:
			return True

		return await self.probe()

	def increment_consumers(self) -> int:
		self.consumer_count += 1
		return self.consumer_count

	def decrement_consumers(self) -> int:
		self.consumer_count = max(0, self.consumer_count - 1)
		return self.consumer_count

	def _start_worker(self) -> None:
		self._stop_worker()

		process_context = cast(Any, self.process_context)
		self.frame_queue = process_context.Queue(maxsize=2)
		self.stop_event = process_context.Event()
		self.process = process_context.Process(
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

	def _prepare_frame(self, frame: np.ndarray) -> np.ndarray:
		return _prepare_tool_camera_frame(frame, self.max_frame_width)

	def _get_frames_to_skip(self, cap: cv2.VideoCapture) -> int:
		source_fps = cap.get(cv2.CAP_PROP_FPS)
		return _get_tool_camera_frames_to_skip(source_fps, self.target_fps)


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
					self.last_frame_at = time.monotonic()

			await asyncio.sleep(self.frame_interval)
	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


camera_manager: Optional[CameraStreamManager] = None
tool_camera_manager: Optional[ToolCameraStreamManager] = None
overhead_camera_manager: Optional[ToolCameraStreamManager] = None

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


async def stop_all_camera_streams() -> None:
	for manager in list(camera_registry.values()):
		manager.consumer_count = 0
		await manager.stop()


def camera_component_name(camera_id: str) -> str:
	return f"{camera_id}_camera"


async def probe_camera_frames(camera_id: str) -> bool:
	manager = camera_registry.get(camera_id)

	if manager is None:
		return False

	await start_camera_stream_by_id(camera_id)

	try:
		return await manager.wait_for_fresh_frame(manager.first_frame_timeout)
	finally:
		await stop_camera_stream_by_id(camera_id)


async def _probe_camera_for_init(camera_id: str) -> None:
	tracker = get_init_tracker()
	component = camera_component_name(camera_id)
	manager = camera_registry.get(camera_id)

	if manager is None:
		tracker.report(component, "failed", reason="camera not registered")
		logger.warning("camera_init_unregistered", camera_id=camera_id)
		return

	tracker.report(component, "connecting")

	try:
		is_delivering_frames = await probe_camera_frames(camera_id)
	except Exception as e:
		tracker.report(component, "failed", reason=str(e))
		logger.warning("camera_init_probe_failed", camera_id=camera_id, error=str(e), exc_info=True)
		return

	if is_delivering_frames is True:
		tracker.report(component, "ready")
		logger.info("camera_init_ready", camera_id=camera_id)
		return

	timeout_s = manager.first_frame_timeout
	tracker.report(component, "failed", reason=f"no frames within {timeout_s:.0f}s")
	logger.warning("camera_init_no_frames", camera_id=camera_id, timeout_s=timeout_s)


async def init_camera_progress(camera_ids: list[str]) -> None:
	if len(camera_ids) == 0:
		return

	tracker = get_init_tracker()
	tracker.register_components([camera_component_name(camera_id) for camera_id in camera_ids])

	await asyncio.gather(*[_probe_camera_for_init(camera_id) for camera_id in camera_ids])


def get_camera_manager_by_id(camera_id: str) -> Optional[CameraManager]:
	return camera_registry.get(camera_id)


async def get_all_cameras_status() -> list[dict]:
	entries = list(camera_registry.items())
	availability = await asyncio.gather(*[manager.is_available() for _, manager in entries])
	cameras = []

	for (camera_id, manager), is_available in zip(entries, availability):
		if isinstance(manager, CameraStreamManager):
			camera_type = getattr(manager, "camera_type", "OpenCvCamera")
		elif camera_id == "overhead":
			camera_type = "OverheadCamera"
		else:
			camera_type = "ToolCamera"

		cameras.append({
			"id": camera_id,
			"type": camera_type,
			"online": is_available,
			"running": manager.is_running,
			"consumers": manager.consumer_count,
			"has_frame": manager.latest_frame is not None
		})

	return cameras
