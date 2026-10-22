from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from multiprocessing.context import BaseContext
from multiprocessing.queues import Queue
from typing import Any, Optional, Protocol, Union, cast
from urllib.parse import urlparse

import av
import cv2
import numpy as np

from openshelf_os_server.core.init_progress import get_init_tracker
from openshelf_os_server.core.logging_config import get_logger

logger = get_logger(__name__)

DEFAULT_RTSP_PORT = 554
RTSP_PROBE_TIMEOUT = 2.0

MIB = 1024 * 1024

# Low-latency ffmpeg client options. CAP_PROP_BUFFERSIZE only bounds OpenCV's
# own frame buffer; these flags bound the demuxer's network buffer (nobuffer/
# low_delay), so they are what keeps the RTSP client from adding hundreds of
# milliseconds of latency and stalling the reader.
FFMPEG_LOW_LATENCY_OPTIONS = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
PYAV_LOW_LATENCY_OPTIONS = {
	"rtsp_transport": "tcp",
	"fflags": "nobuffer",
	"flags": "low_delay",
}

# NALU access-unit ring. The crash recorder freezes the lookback window from
# here, so the ring must outlive it. 8 MiB caps a QHD main stream at ~20 s.
NALE_QUEUE_MAXSIZE = 256
NALE_RING_SECONDS = 20.0
NALE_RING_MAX_BYTES = 8 * MIB
NALE_CATCHUP_SECONDS = 2.0

# Worker queue item: (source, kind, pts_seconds, arrival_seconds, payload).
# source: "view" | "record"; kind: "au" (Annex-B access unit), "config"
# (h264 avcC extradata) or "meta" ((width, height, fps, codec name)).
NaluQueueItem = tuple[str, str, float, float, Any]

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


async def _wait_until(predicate, timeout: float) -> bool:
	deadline = time.monotonic() + timeout

	while time.monotonic() < deadline:
		if predicate() is True:
			return True

		await asyncio.sleep(0.05)

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


def _encode_nalu_frames(access_units: list[bytes]) -> bytes:
	"""Frame access units for WebSocket transport: 4-byte big-endian length + AU.

	Length prefixes keep multi-AU messages unambiguous even when an AU's own
	bytes contain an Annex-B start code.
	"""
	out = bytearray()

	for unit in access_units:
		out += struct.pack(">I", len(unit))
		out += unit

	return bytes(out)


def _split_annexb_nalus(data: bytes) -> list[bytes]:
	"""Split an Annex-B byte string into NAL units, start codes stripped.

	Any zero run of length >= 2 followed by 0x01 is a start code, no
	matter how long the run is (00 0 0 01, 00 0 0 0 1, and longer);
	the boundary is recorded at the start of the zero run. A byte string
	without any start code is treated as a single NAL.
	"""
	boundaries: list[tuple[int, int]] = []  # (start_code_start, payload_start)
	i = 0
	n = len(data)

	while i < n:
		if data[i] == 0:
			j = i
			while j < n and data[j] == 0:
				j += 1
			if j < n and data[j] == 1 and (j - i) >= 2:
				boundaries.append((i, j + 1))
				i = j + 1
				continue
		i += 1

	if len(boundaries) == 0:
		return [data] if len(data) > 0 else []

	units: list[bytes] = []

	if boundaries[0][0] > 0:
		units.append(bytes(data[: boundaries[0][0]]))

	for index, (_code_start, payload_start) in enumerate(boundaries):
		end = boundaries[index + 1][0] if index + 1 < len(boundaries) else n
		chunk = data[payload_start:end]
		if len(chunk) > 0:
			units.append(bytes(chunk))

	return units


def _annexb_to_avcc_au(unit: bytes) -> bytes:
	"""Rewrite an Annex-B access unit as AVCC: u32 length + NAL per NAL.

	WebCodecs reads 4-byte length-prefixed NALs whenever the decoder
	config carries a `description`, so the browser fan-out must strip the
	ring's Annex-B start codes. The rings themselves keep Annex-B.
	"""
	out = bytearray()

	for nalu in _split_annexb_nalus(unit):
		out += struct.pack(">I", len(nalu))
		out += nalu

	return bytes(out)


def _au_contains_idr(unit: bytes, au_is_avcc: bool) -> bool:
	"""Whether an access unit contains an IDR slice NAL (type 5).

	The ring may hold either wire form: Annex-B (start-code NALs) or
	AVCC (u32 length-prefixed NALs).
	"""
	if au_is_avcc:
		offset = 0

		while offset + 4 <= len(unit):
			length = struct.unpack_from(">I", unit, offset)[0]
			header = offset + 4
			if length == 0 or header + length > len(unit):
				break
			if unit[header] & 0x1F == 5:
				return True
			offset = header + length

		return False

	for nalu in _split_annexb_nalus(unit):
		if nalu and nalu[0] & 0x1F == 5:
			return True

	return False


def _au_contains_hevc_keyframe(unit: bytes) -> bool:
	"""Whether an access unit contains a HEVC key-frame (IRAP) slice NAL.

	HEVC carries the 6-bit NAL unit type in the top six bits of the
	first byte. Any of 16-21 (BLA_W_LP, BLA_W_RADL, BLA_N_LP, IDR_W_RADL,
	IDR_N_LP, CRA_NUT) is an instantaneous random
	access point a decoder can start from, and encoders use different
	members of that range for the first key frame than for later ones
	(e.g. libx265), so the gate must accept the whole range. HEVC
	over RTSP is always Annex-B here, so there is no AVCC form to
	handle.
	"""
	for nal_unit in _split_annexb_nalus(unit):
		if nal_unit and (nal_unit[0] >> 1) & 0x3F in range(16, 22):
			return True

	return False


@dataclass(frozen=True)
class NaluCodecMeta:
	codec: str
	description: bytes
	width: int
	height: int
	fps: float
	# Whether the stream's access units are already AVCC (4-byte
	# length-prefixed NALs). Annex-B AUs must be converted before the
	# WebCodecs fan-out; the rings keep Annex-B either way.
	au_is_avcc: bool


def _parse_avcc_meta(extradata: bytes, width: int, height: int, fps: float) -> Optional[NaluCodecMeta]:
	"""Build the WebCodecs codec string from h264 avcC extradata.

	The first three bytes of an avcC record are profile_idc, the
	0xF0-masked constraint byte, and level_idc, which is exactly the
	avc1.PPPLLL triplet.
	"""
	if len(extradata) >= 4 and extradata[0] == 1:
		return NaluCodecMeta(
			codec="avc1.%02X%02X%02X" % (extradata[1], extradata[2], extradata[3]),
			description=extradata,
			width=width,
			height=height,
			fps=fps,
			au_is_avcc=True,
		)

	return _parse_annexb_meta(extradata, width, height, fps)


def _parse_annexb_meta(extradata: bytes, width: int, height: int, fps: float) -> Optional[NaluCodecMeta]:
	"""Build the WebCodecs meta from Annex-B SPS/PPS extradata.

	Over RTSP, PyAV/ffmpeg expose the h264 `extradata` as Annex-B (SPS
	and PPS with 00-00-01 start codes) rather than avcC, so synthesize
	a real avcC record for the WS header and mark the stream's access
	units as Annex-B for the browser fan-out conversion.
	"""
	if len(extradata) < 4 or not (extradata[:4] == b"\x00\x00\x00\x01" or extradata[:3] == b"\x00\x00\x01"):
		return None

	sps: bytes = b""
	pps: bytes = b""

	for nalu in _split_annexb_nalus(extradata):
		nalu_type = nalu[0] & 0x1F
		if nalu_type == 7 and len(sps) == 0:
			sps = nalu
		elif nalu_type == 8 and len(pps) == 0:
			pps = nalu

	if len(sps) < 4 or len(pps) == 0:
		return None

	# avcC (ISO/IEC 1449-5): version, profile/compat/level,
	# 0b11111100|lengthSizeMinusOne=3, 0bE0|(numSPS-1)=1 SPS,
	# u16 SPS length + SPS, numPPS=1, u16 PPS length + PPS.
	avc_c = (
		b"\x01" + sps[1:4]
		+ b"\xff\xe1"
		+ struct.pack(">H", len(sps)) + sps
		+ b"\x01" + struct.pack(">H", len(pps)) + pps
	)

	return NaluCodecMeta(
		codec="avc1.%02X%02X%02X" % (sps[1], sps[2], sps[3]),
		description=avc_c,
		width=width,
		height=height,
		fps=fps,
		au_is_avcc=False,
	)


def _nalu_queue_put(nalu_queue: "Queue", item: NaluQueueItem) -> None:
	# Drop-oldest keeps the live stream current if the main process stalls.
	try:
		nalu_queue.put_nowait(item)
	except queue.Full:
		try:
			nalu_queue.get_nowait()
		except queue.Empty:
			pass
		try:
			nalu_queue.put_nowait(item)
		except queue.Full:
			pass


def _nalu_source_reader(
	source: str,
	rtsp_url: str,
	nalu_queue: "Queue",
	stop_event: multiprocessing.synchronize.Event,
) -> None:
	backoff = 2.0

	while stop_event.is_set() is False:
		delivered = False

		try:
			container = av.open(rtsp_url, options=PYAV_LOW_LATENCY_OPTIONS)
		except Exception:
			container = None

		if container is None:
			time.sleep(backoff)
			backoff = min(30.0, backoff * 2.0)
			continue

		last_extradata: bytes | None = None

		try:
			stream = container.streams.video[0]
			codec_context = stream.codec_context
			_nalu_queue_put(nalu_queue, (source, "meta", 0.0, 0.0, (
				int(codec_context.width or 0),
				int(codec_context.height or 0),
				float(stream.average_rate) if stream.average_rate else 0.0,
				codec_context.name,
			)))

			for packet in container.demux(stream):
				if stop_event.is_set():
					break

				data = bytes(packet)
				if len(data) == 0:
					continue

				if packet.pts is not None and stream.time_base is not None:
					try:
						pts = float(packet.pts * stream.time_base)
					except (OverflowError, ZeroDivisionError):
						pts = time.monotonic()
				else:
					pts = time.monotonic()

				delivered = True
				_nalu_queue_put(nalu_queue, (source, "au", pts, time.monotonic(), data))

				# SPS/PPS arrive with the first in-band parameter sets; ffmpeg
				# surfaces them as extradata once seen.
				extradata = codec_context.extradata
				if extradata is not None:
					raw = bytes(extradata)
					if raw != last_extradata:
						last_extradata = raw
						_nalu_queue_put(nalu_queue, (source, "config", 0.0, 0.0, raw))
		except Exception:
			pass
		finally:
			try:
				container.close()
			except Exception:
				pass

		if stop_event.is_set() is True:
			return

		if delivered is False:
			time.sleep(backoff)
			backoff = min(30.0, backoff * 2.0)
		else:
			backoff = 2.0


def _nalu_worker(
	view_rtsp_url: str,
	recording_rtsp_url: str,
	nalu_queue: "Queue",
	stop_event: multiprocessing.synchronize.Event,
) -> None:
	# Each URL reads in its own thread so one stalled camera cannot hold the
	# other's access units.
	readers: list[threading.Thread] = [
		threading.Thread(
			target=_nalu_source_reader,
			args=("view", view_rtsp_url, nalu_queue, stop_event),
			daemon=True,
		)
	]

	if recording_rtsp_url != "":
		readers.append(
			threading.Thread(
				target=_nalu_source_reader,
				args=("record", recording_rtsp_url, nalu_queue, stop_event),
				daemon=True,
			)
		)

	for reader in readers:
		reader.start()

	stop_event.wait()

	for reader in readers:
		reader.join(timeout=2.0)


def _tool_camera_worker(
	rtsp_url: str,
	frame_queue: Queue,
	stop_event: multiprocessing.synchronize.Event,
	target_fps: int,
	max_frame_width: int,
	jpeg_quality: int,
) -> None:
	os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", FFMPEG_LOW_LATENCY_OPTIONS)
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
		self.background_consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.last_frame_at: Optional[float] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		self.target_fps = 12
		self.background_target_fps = 3
		self.frame_interval = 1.0 / self.target_fps
		self.jpeg_quality = 70
		self.first_frame_timeout = 5.0
		self.stale_frame_timeout = 5.0
		self.lifecycle_lock = asyncio.Lock()
		self.unregistered = asyncio.Event()
		# Event and exception type of the current capture failure streak, its length, and whether its first
		# failure was already reported at ERROR.
		self.capture_failure_kind: Optional[str] = None
		self.capture_failure_streak = 0
		self.capture_failure_reported = False

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

	def mjpeg_path_stale(self) -> bool:
		# Staleness of the MJPEG path only: it never consults the NALU pipe,
		# so a dead NALU worker cannot mark a healthy MJPEG stream as stalled.
		if self.is_running is False:
			return True

		if self.last_frame_at is None:
			return True

		return (time.monotonic() - self.last_frame_at) > self.stale_frame_timeout

	def has_frame(self) -> bool:
		return self.latest_frame is not None

	async def wait_for_fresh_frame(self, timeout: float) -> bool:
		return await _wait_until(lambda: self.is_frame_stale() is False, timeout)

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

	def increment_background_consumers(self) -> int:
		self.background_consumer_count += 1
		return self.background_consumer_count

	def decrement_background_consumers(self) -> int:
		self.background_consumer_count = max(0, self.background_consumer_count - 1)
		return self.background_consumer_count

	def effective_target_fps(self) -> int:
		if self.consumer_count > 0:
			return self.target_fps
		return min(self.target_fps, self.background_target_fps)

	def _capture_and_encode(self, camera: RawCamera) -> Optional[bytes]:
		"""Blocking read + JPEG encode. Runs in the default executor so the
		encode never blocks the API event loop."""
		frame = camera.capture_raw()
		if frame is None:
			return None

		success, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])

		if success is False:
			return None

		return buffer.tobytes()

	async def _capture_loop(self) -> None:
		while self.is_running is True:
			loop_start = asyncio.get_event_loop().time()

			try:
				if self.camera is None:
					await asyncio.sleep(1.0)
					continue

				camera = self.camera
				loop = asyncio.get_event_loop()
				frame_bytes = await loop.run_in_executor(None, self._capture_and_encode, camera)

				if frame_bytes is None:
					await asyncio.sleep(1.0)
					continue

				async with self.frame_lock:
					self.latest_frame = frame_bytes
					self.last_frame_at = time.monotonic()

				if self.capture_failure_streak > 0:
					logger.info(
						"camera_capture_recovered",
						camera_type=self.camera_type,
						failed_attempts=self.capture_failure_streak,
					)
					self.capture_failure_streak = 0
					self.capture_failure_reported = False

			except RuntimeError as e:
				if "bad cam" in str(e):
					self.report_capture_failure("camera_connection_failed", e)
					await asyncio.sleep(5.0)
				else:
					self.report_capture_failure("camera_runtime_error", e, exc_info=True)
					await asyncio.sleep(1.0)
				continue
			except Exception as e:
				self.report_capture_failure(
					"camera_frame_processing_error", e, exc_info=True, error_type=type(e).__name__
				)
				await asyncio.sleep(1.0)
				continue

			elapsed = asyncio.get_event_loop().time() - loop_start
			sleep_time = max(0.0, (1.0 / self.effective_target_fps()) - elapsed)
			await asyncio.sleep(sleep_time)

	def report_capture_failure(self, event: str, error: BaseException, exc_info: bool = False, **fields: Any) -> None:
		"""Log a capture failure at ERROR (one Sentry event) only for the first of a streak.

		The loop retries a broken camera every second, so retries log at WARNING until the
		recovery event; a different event or exception type starts a new streak.
		"""
		failure_kind = f"{event}:{type(error).__name__}"

		if self.capture_failure_kind != failure_kind:
			self.capture_failure_kind = failure_kind
			self.capture_failure_streak = 0
			self.capture_failure_reported = False

		self.capture_failure_streak += 1

		if self.capture_failure_reported is True:
			logger.warning(
				event,
				camera_type=self.camera_type,
				error=str(error),
				failed_attempts=self.capture_failure_streak,
				**fields,
			)
			return

		self.capture_failure_reported = True
		logger.error(event, camera_type=self.camera_type, error=str(error), exc_info=exc_info, **fields)

	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


class ToolCameraStreamManager:
	def __init__(self, rtsp_url: str, recording_url: str = ""):
		self.rtsp_url = rtsp_url
		self.recording_url = recording_url
		self.is_running = False
		self.consumer_count = 0
		self.nalu_consumer_count = 0
		self.background_consumer_count = 0
		self.latest_frame: Optional[bytes] = None
		self.last_frame_at: Optional[float] = None
		self.frame_lock = asyncio.Lock()
		self.capture_task: Optional[asyncio.Task] = None
		# On-demand MJPEG decode worker (non-WebCodecs browser clients only).
		self.process: Optional[multiprocessing.Process] = None
		self.stop_event: Optional[multiprocessing.synchronize.Event] = None
		self.frame_queue: Optional[Queue] = None
		self.process_context: BaseContext = multiprocessing.get_context("spawn")
		self.target_fps = 15
		self.frame_interval = 1.0 / self.target_fps
		self.max_frame_width = 640
		self.jpeg_quality = 65
		self.first_frame_timeout = 15.0
		self.stale_frame_timeout = 10.0
		# NALU pipe: the 24/7 path. No H.264 decode on the Pi, ever.
		self.nalu_process: Optional[multiprocessing.Process] = None
		self.nalu_stop_event: Optional[multiprocessing.synchronize.Event] = None
		self.nalu_queue: Optional[Queue] = None
		self.nalu_process_context: BaseContext = self.process_context
		self.nalu_view_ring: deque = deque()
		self.nalu_record_ring: deque = deque()
		self.nalu_ring_bytes = {"view": 0, "record": 0}
		self.nalu_last_frame_at: Optional[float] = None
		self.nalu_extradata: Optional[bytes] = None
		self.nalu_meta: Optional[tuple[int, int, float, str]] = None
		self.nalu_view_codec: Optional[str] = None
		self.nalu_record_codec: Optional[str] = None
		self.nalu_subscribers: set[asyncio.Queue] = set()
		self.lifecycle_lock = asyncio.Lock()
		self.unregistered = asyncio.Event()

	async def start(self) -> None:
		async with self.lifecycle_lock:
			if self.is_running is True:
				return

			self.latest_frame = None
			self.last_frame_at = None
			self.nalu_last_frame_at = None
			self.nalu_view_codec = None
			self.nalu_record_codec = None
			self.is_running = True
			try:
				self._start_nalu_worker()
			except BaseException:
				# Roll back: the MJPEG fallback only (re)starts when
				# is_running is False, and no worker exists yet.
				self.is_running = False
				raise
			self.capture_task = asyncio.create_task(self._capture_loop())
			logger.info(
				"tool_camera_ready",
				rtsp_url=self.rtsp_url,
				has_recording_url=self.recording_url != "",
			)

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

			for subscriber in list(self.nalu_subscribers):
				self._close_nalu_subscriber(subscriber)
				self.nalu_subscribers.discard(subscriber)

			self._stop_worker()
			self._stop_nalu_worker()
			self.latest_frame = None
			self.last_frame_at = None
			self.nalu_last_frame_at = None
			self.nalu_view_ring.clear()
			self.nalu_record_ring.clear()
			self.nalu_ring_bytes = {"view": 0, "record": 0}

	def is_frame_stale(self) -> bool:
		if self.nalu_last_frame_at is None:
			if self.last_frame_at is None:
				return True

			return (time.monotonic() - self.last_frame_at) > self.stale_frame_timeout

		return (time.monotonic() - self.nalu_last_frame_at) > self.stale_frame_timeout

	def mjpeg_path_stale(self) -> bool:
		# Staleness of the MJPEG path only: it never consults the NALU pipe,
		# so a dead NALU worker cannot mark a healthy MJPEG stream as stalled.
		if self.is_running is False:
			return True

		if self.last_frame_at is None:
			return True

		return (time.monotonic() - self.last_frame_at) > self.stale_frame_timeout

	def has_frame(self) -> bool:
		if len(self.nalu_view_ring) > 0 or len(self.nalu_record_ring) > 0:
			return True

		return self.latest_frame is not None

	async def wait_for_fresh_frame(self, timeout: float) -> bool:
		# MJPEG consumers need the decoded JPEG path to be delivering.
		return await _wait_until(
			lambda: self.last_frame_at is not None and (time.monotonic() - self.last_frame_at) <= self.stale_frame_timeout,
			timeout,
		)

	async def wait_for_nalu_ready(self, timeout: float) -> bool:
		return await _wait_until(
			lambda: self.nalu_last_frame_at is not None and (time.monotonic() - self.nalu_last_frame_at) <= self.stale_frame_timeout,
			timeout,
		)

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

	def increment_nalu_consumers(self) -> int:
		self.nalu_consumer_count += 1
		return self.nalu_consumer_count

	def decrement_nalu_consumers(self) -> int:
		self.nalu_consumer_count = max(0, self.nalu_consumer_count - 1)
		return self.nalu_consumer_count

	def increment_background_consumers(self) -> int:
		self.background_consumer_count += 1
		return self.background_consumer_count

	def decrement_background_consumers(self) -> int:
		self.background_consumer_count = max(0, self.background_consumer_count - 1)
		return self.background_consumer_count

	# NALU pipe (crash recorder + H.264 WebSocket stream)

	def _start_nalu_worker(self) -> None:
		if self.nalu_process is not None and self.nalu_process.is_alive() is True:
			return

		context = cast(Any, self.nalu_process_context)
		self.nalu_queue = context.Queue(maxsize=NALE_QUEUE_MAXSIZE)
		self.nalu_stop_event = context.Event()
		self.nalu_process = context.Process(
			target=_nalu_worker,
			args=(
				self.rtsp_url,
				self.recording_url,
				self.nalu_queue,
				self.nalu_stop_event,
			),
			daemon=True,
		)
		self.nalu_process.start()
		logger.info(
			"tool_camera_nalu_worker_started",
			rtsp_url=self.rtsp_url,
			pid=self.nalu_process.pid,
		)

	def _stop_nalu_worker(self) -> None:
		if self.nalu_stop_event is not None:
			self.nalu_stop_event.set()

		if self.nalu_process is not None:
			# The worker only decodes frames; one blocked in an RTSP read never sees the
			# stop event, so do not hold shutdown for it.
			self.nalu_process.join(timeout=0.5)
			if self.nalu_process.is_alive() is True:
				self.nalu_process.terminate()
				self.nalu_process.join(timeout=2.0)
			if self.nalu_process.is_alive() is True:
				self.nalu_process.kill()
				self.nalu_process.join(timeout=1.0)

		self.nalu_process = None
		self.nalu_stop_event = None
		self.nalu_queue = None

	def _drain_nalu_queue(self) -> list[bytes]:
		"""Move queued items into the rings; returns this tick's view AUs."""
		tick_aus: list[bytes] = []

		if self.nalu_queue is None:
			return tick_aus

		while True:
			try:
				source, kind, pts, _arrival, payload = self.nalu_queue.get_nowait()
			except queue.Empty:
				break

			if kind == "au":
				ring = self.nalu_record_ring if source == "record" else self.nalu_view_ring
				ring.append((pts, payload, _arrival))
				byte_counter = self.nalu_ring_bytes
				byte_counter[source] = byte_counter[source] + len(payload)
				cutoff = time.monotonic() - NALE_RING_SECONDS
				while len(ring) > 0:
					_oldest_pts, _oldest_data, oldest_arrival = ring[0]
					if byte_counter[source] <= NALE_RING_MAX_BYTES and oldest_arrival >= cutoff:
						break
					removed = ring.popleft()
					byte_counter[source] = byte_counter[source] - len(removed[1])
				if source == "view":
					# View AUs alone drive the freshness signal; record traffic must
					# not mask a dead view stream (liveness, init probe, WS gate).
					self.nalu_last_frame_at = _arrival
					tick_aus.append(payload)
			elif kind == "config" and source == "view":
				self.nalu_extradata = payload
			elif kind == "meta" and source == "view":
				self.nalu_meta = payload
				self.nalu_view_codec = payload[3]
			elif kind == "meta" and source == "record":
				self.nalu_record_codec = payload[3]

		return tick_aus

	def _drain_frame_queue(self) -> Optional[bytes]:
		if self.frame_queue is None:
			return None

		latest_frame = None

		while True:
			try:
				latest_frame = self.frame_queue.get_nowait()
			except queue.Empty:
				return latest_frame

	def _close_nalu_subscriber(self, subscriber: asyncio.Queue) -> None:
		try:
			subscriber.put_nowait(None)
		except asyncio.QueueFull:
			try:
				subscriber.get_nowait()
			except asyncio.QueueEmpty:
				pass
			try:
				subscriber.put_nowait(None)
			except asyncio.QueueFull:
				pass

	def get_nalu_window(self, seconds: float, source: str = "record") -> list[tuple[float, bytes]]:
		if source == "record" and self.recording_url != "":
			ring = self.nalu_record_ring
		else:
			ring = self.nalu_view_ring
		cutoff = time.monotonic() - seconds
		return [(pts, data) for (pts, data, arrival) in ring if arrival >= cutoff]

	def nalu_window_codec(self) -> Optional[str]:
		# Mirrors get_nalu_window's source selection exactly: the frozen
		# window holds the ring that source names, so the codec must
		# come from that same source. The source's meta may still be
		# missing or evicted, in which case the window has no known
		# codec and the recorder degrades to its explicit-error path
		# instead of labeling the ring with the other source's codec.
		if self.recording_url != "":
			return self.nalu_record_codec

		return self.nalu_view_codec

	def nalu_codec_meta(self) -> Optional[NaluCodecMeta]:
		if self.nalu_extradata is None or self.nalu_meta is None:
			return None

		width, height, fps, _codec_name = self.nalu_meta

		return _parse_avcc_meta(self.nalu_extradata, width, height, fps)

	def _nalu_au_is_annexb(self) -> bool:
		"""Whether the ring's AUs need Annex-B to AVCC conversion.

		WebCodecs reads length-prefixed NALs whenever the decoder config
		carries a description, but over RTSP ffmpeg frames the extradata
		(and the ring's access units) in Annex-B. Codec meta unknown
		means the ring's AUs are Annex-B until the container says
		otherwise.
		"""
		meta = self.nalu_codec_meta()
		if meta is None:
			return True
		return meta.au_is_avcc is False

	def add_nalu_subscriber(self) -> asyncio.Queue:
		subscriber: asyncio.Queue = asyncio.Queue(maxsize=64)

		# Catch-up window so the browser can start decoding immediately.
		# WebCodecs rejects a decoder whose first chunk is not 'key',
		# and the window's oldest entry is normally a mid-GOP delta, so
		# anchor the replay at the window's newest IDR access unit.
		cutoff = time.monotonic() - NALE_CATCHUP_SECONDS
		au_is_annexb = self._nalu_au_is_annexb()
		window: list[bytes] = []
		idr_index = 0

		for _pts, data, arrival in self.nalu_view_ring:
			if arrival < cutoff:
				continue
			if _au_contains_idr(data, au_is_annexb is False):
				idr_index = len(window)
			window.append(data)

		for data in window[idr_index:]:
			if au_is_annexb is True:
				data = _annexb_to_avcc_au(data)
			try:
				subscriber.put_nowait(_encode_nalu_frames([data]))
			except asyncio.QueueFull:
				break

		self.nalu_subscribers.add(subscriber)
		return subscriber

	def remove_nalu_subscriber(self, subscriber: asyncio.Queue) -> None:
		self.nalu_subscribers.discard(subscriber)

	# On-demand MJPEG decode worker (non-WebCodecs browser clients). The
	# 24/7 path is the NALU pipe, which never decodes H.264.

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
			# The worker only decodes frames; one blocked in an RTSP read never sees the
			# stop event, so do not hold shutdown for it.
			self.process.join(timeout=0.5)
			if self.process.is_alive() is True:
				self.process.terminate()
				self.process.join(timeout=2.0)
			if self.process.is_alive() is True:
				self.process.kill()
				self.process.join(timeout=1.0)

		self.process = None
		self.stop_event = None
		self.frame_queue = None

	async def _capture_loop(self) -> None:
		mjpeg_restart_delay = 1.0
		nalu_restart_delay = 1.0
		nalu_was_restarted = False

		while self.is_running is True:
			want_mjpeg = self.consumer_count > 0

			if want_mjpeg is True and self.process is None:
				self._start_worker()
			elif want_mjpeg is False and self.process is not None:
				self._stop_worker()

			# NALU worker liveness, with escalating restart backoff. A failed
			# spawn must never end the capture loop.
			try:
				if self.nalu_process is None or self.nalu_process.is_alive() is False:
					exitcode = None if self.nalu_process is None else self.nalu_process.exitcode
					logger.error(
						"tool_camera_nalu_worker_exited",
						rtsp_url=self.rtsp_url,
						exitcode=exitcode,
						restart_delay=nalu_restart_delay,
					)
					await asyncio.sleep(nalu_restart_delay)
					if self.is_running is False:
						return
					nalu_restart_delay = min(30.0, nalu_restart_delay * 2.0)
					self._start_nalu_worker()
					nalu_was_restarted = True
					continue

				if nalu_was_restarted is True:
					# A respawned worker stayed alive across a tick.
					nalu_was_restarted = False
					nalu_restart_delay = 1.0

				tick_aus = self._drain_nalu_queue()
			except Exception as e:
				logger.warning(
					"tool_camera_nalu_worker_spawn_failed",
					rtsp_url=self.rtsp_url,
					error=str(e),
					error_type=type(e).__name__,
					retry_delay=nalu_restart_delay,
				)
				await asyncio.sleep(nalu_restart_delay)
				continue

			# The rings keep Annex-B for the recorder; the AVCC conversion,
			# framing, and fan-out are browser-subscriber work and cost a
			# slice of a core per camera, so skip them with no subscribers.
			if len(tick_aus) > 0 and len(self.nalu_subscribers) > 0:
				if self._nalu_au_is_annexb() is True:
					tick_aus = [_annexb_to_avcc_au(unit) for unit in tick_aus]
				message = _encode_nalu_frames(tick_aus)
				for subscriber in list(self.nalu_subscribers):
					try:
						subscriber.put_nowait(message)
					except asyncio.QueueFull:
						try:
							subscriber.get_nowait()
						except asyncio.QueueEmpty:
							pass
						try:
							subscriber.put_nowait(message)
						except asyncio.QueueFull:
							pass

			# On-demand MJPEG decode worker.
			if self.process is not None:
				if self.process.is_alive() is False:
					logger.error(
						"tool_camera_worker_exited",
						rtsp_url=self.rtsp_url,
						exitcode=self.process.exitcode,
						restart_delay=mjpeg_restart_delay,
					)
					await asyncio.sleep(mjpeg_restart_delay)
					if self.is_running is False:
						return
					self._start_worker()
					mjpeg_restart_delay = min(30.0, mjpeg_restart_delay * 2.0)
					continue

				if want_mjpeg is True:
					frame = self._drain_frame_queue()
					if frame is not None:
						# A delivering worker resets the death backoff to its
						# initial value, so an old death can't starve later
						# restarts (and the tick's queue drain and fan-out)
						# for up to 30 seconds.
						mjpeg_restart_delay = 1.0
						async with self.frame_lock:
							self.latest_frame = frame
							self.last_frame_at = time.monotonic()

			await asyncio.sleep(0.025)

	async def get_latest_frame(self) -> Optional[bytes]:
		async with self.frame_lock:
			return self.latest_frame


tool_camera_manager: Optional[ToolCameraStreamManager] = None
overhead_camera_manager: Optional[ToolCameraStreamManager] = None


async def init_sizer_camera_manager(camera: RawCamera) -> None:
	camera_registry["iobox_sizer"] = CameraStreamManager(camera=camera, camera_type="IOBoxSizerCamera")
	logger.info("iobox_sizer_camera_registered")


async def unregister_camera_manager(camera_id: str) -> None:
	manager = camera_registry.pop(camera_id, None)

	if manager is None:
		return

	manager.unregistered.set()
	await manager.stop()


async def init_tool_camera_manager(rtsp_url: str, recording_url: str = "") -> None:
	global tool_camera_manager, camera_registry

	if rtsp_url is None or rtsp_url == "":
		logger.warning("tool_camera_not_configured", reason="no_rtsp_url")
		return

	tool_camera_manager = ToolCameraStreamManager(rtsp_url=rtsp_url, recording_url=recording_url)
	camera_registry["tool"] = tool_camera_manager
	logger.info("tool_camera_registered", rtsp_url=rtsp_url)


async def init_overhead_camera_manager(rtsp_url: str, recording_url: str = "") -> None:
	global overhead_camera_manager, camera_registry

	if rtsp_url is None or rtsp_url == "":
		logger.warning("overhead_camera_not_configured", reason="no_rtsp_url")
		return

	overhead_camera_manager = ToolCameraStreamManager(rtsp_url=rtsp_url, recording_url=recording_url)
	camera_registry["overhead"] = overhead_camera_manager
	logger.info("overhead_camera_registered", rtsp_url=rtsp_url)


async def init_overview_camera_manager(camera_id: str, rtsp_url: str, recording_url: str = "") -> None:
	if camera_id not in {"overview_one", "overview_two"}:
		raise ValueError(f"Unsupported overview camera id: {camera_id}")
	if rtsp_url is None or rtsp_url == "":
		logger.warning("overview_camera_not_configured", camera_id=camera_id, reason="no_rtsp_url")
		return

	camera_registry[camera_id] = ToolCameraStreamManager(rtsp_url=rtsp_url, recording_url=recording_url)
	logger.info(
		"overview_camera_registered",
		camera_id=camera_id,
		rtsp_url=rtsp_url,
		has_recording_url=recording_url != "",
	)


def _all_consumer_counts(manager: CameraManager) -> tuple[int, int, int]:
	return (
		manager.consumer_count,
		getattr(manager, "nalu_consumer_count", 0),
		manager.background_consumer_count,
	)


async def start_camera_stream_by_id(camera_id: str, *, background: bool = False) -> None:
	manager = camera_registry.get(camera_id)

	if manager is None:
		raise ValueError(f"Camera '{camera_id}' not found")

	if background is True:
		manager.increment_background_consumers()
	else:
		manager.increment_consumers()

	try:
		if manager.is_running is False:
			await manager.start()
	except BaseException:
		if background is True:
			manager.decrement_background_consumers()
		else:
			manager.decrement_consumers()
		raise


async def stop_camera_stream_by_id(camera_id: str, *, background: bool = False) -> None:
	manager = camera_registry.get(camera_id)

	if manager is None:
		return

	if background is True:
		manager.decrement_background_consumers()
	else:
		manager.decrement_consumers()

	mjpeg_consumers, nalu_consumers, background_consumers = _all_consumer_counts(manager)

	if (
		mjpeg_consumers == 0
		and nalu_consumers == 0
		and background_consumers == 0
		and manager.is_running is True
	):
		await manager.stop()


async def start_nalu_stream_by_id(camera_id: str) -> None:
	manager = camera_registry.get(camera_id)

	if manager is None or not isinstance(manager, ToolCameraStreamManager):
		raise ValueError(f"Camera '{camera_id}' not found")

	manager.increment_nalu_consumers()

	try:
		if manager.is_running is False:
			await manager.start()
	except BaseException:
		manager.decrement_nalu_consumers()
		raise


async def stop_nalu_stream_by_id(camera_id: str) -> None:
	manager = camera_registry.get(camera_id)

	if manager is None or not isinstance(manager, ToolCameraStreamManager):
		return

	manager.decrement_nalu_consumers()

	mjpeg_consumers, nalu_consumers, background_consumers = _all_consumer_counts(manager)

	if (
		mjpeg_consumers == 0
		and nalu_consumers == 0
		and background_consumers == 0
		and manager.is_running is True
	):
		await manager.stop()


async def stop_all_camera_streams() -> None:
	for manager in list(camera_registry.values()):
		manager.consumer_count = 0
		manager.background_consumer_count = 0
		await manager.stop()


def camera_component_name(camera_id: str) -> str:
	return f"{camera_id}_camera"


async def probe_camera_frames(camera_id: str) -> bool:
	manager = camera_registry.get(camera_id)

	if manager is None:
		return False

	if isinstance(manager, ToolCameraStreamManager):
		# The NALU pipe proves reachability without a full H.264 decode.
		await start_nalu_stream_by_id(camera_id)

		try:
			return await manager.wait_for_nalu_ready(manager.first_frame_timeout)
		finally:
			await stop_nalu_stream_by_id(camera_id)

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
	tracker.register_components(
		[camera_component_name(camera_id) for camera_id in camera_ids],
		degrade=False,
	)

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
		elif camera_id in {"overview_one", "overview_two"}:
			camera_type = "OverviewCamera"
		else:
			camera_type = "ToolCamera"

		cameras.append({
			"id": camera_id,
			"type": camera_type,
			"online": is_available,
			"running": manager.is_running,
			"consumers": manager.consumer_count,
			"has_frame": manager.has_frame(),
		})

	return cameras
