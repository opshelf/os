"""On-demand camera recording to disk.

Records one camera source (the RTSP view stream or the higher-resolution
recording stream) into an MP4 file by remuxing the NALU access units the
stream manager already demuxes. No re-encode and no second RTSP session:
the units are forwarded from the manager's drain loop into a bounded queue
that a worker thread drains, so a slow writer drops units instead of
blocking the live path.
"""
from __future__ import annotations

import asyncio
import io
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, cast

import av
import cv2
import numpy as np

from openshelf_os_server.core.camera import _au_contains_hevc_keyframe, _au_contains_idr
from openshelf_os_server.core.logging_config import get_logger

logger = get_logger(__name__)

RECORDINGS_DIRNAME = "camera_recordings"
NALE_DEMUX_FORMAT_BY_CODEC = {"h264": "h264", "hevc": "hevc"}
# The optional -N suffix marks the Nth take allocated inside one timestamp
# second (see CameraRecordingStore.new_file_path). Listing, download, and
# delete all gate on this pattern, so it has to accept those names.
FILE_NAME_RE = re.compile(r"^\d{12}-[a-z0-9_]{1,32}(-\d+)?\.mp4$")
CAMERA_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")

RECORDER_QUEUE_MAXSIZE = 1024
RECORD_DEFAULT_FPS = 25.0


def _unit_has_key(unit: bytes, codec_name: str) -> bool:
	if codec_name == "hevc":
		return _au_contains_hevc_keyframe(unit)

	return _au_contains_idr(unit, False) or _au_contains_idr(unit, True)


def _first_key_unit_index(units: list[bytes], codec_name: str) -> int:
	"""Index of the first key access unit in a batch, or -1 when it has none."""
	for index, unit in enumerate(units):
		if _unit_has_key(unit, codec_name):
			return index

	return -1


def _batch_has_key(units: list[bytes], codec_name: str) -> bool:
	return _first_key_unit_index(units, codec_name) >= 0


def _elapsed_status_fields(started_at: float) -> dict[str, Any]:
	elapsed = max(0.0, time.monotonic() - started_at)
	return {
		"started_at": (datetime.now(timezone.utc) - timedelta(seconds=elapsed)).isoformat(),
		"elapsed_s": round(elapsed, 1),
	}


class CameraDiskRecorder:
	"""Records NALU access units into one MP4 file without re-encoding."""

	def __init__(
		self,
		camera_id: str,
		source: str,
		path: Path,
		max_duration_s: Optional[int] = None,
		codec_name: Optional[str] = None,
		fps: float = RECORD_DEFAULT_FPS,
	) -> None:
		self.camera_id = camera_id
		self.source = source
		self.path = path
		self.max_duration_s = max_duration_s
		self.started_at = time.monotonic()
		self._codec_name = codec_name if codec_name in NALE_DEMUX_FORMAT_BY_CODEC else "h264"
		self._fps = fps if fps > 0 else RECORD_DEFAULT_FPS
		self._width: Optional[int] = None
		self._height: Optional[int] = None
		self._au_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=RECORDER_QUEUE_MAXSIZE)
		self._stop_event = threading.Event()
		self._finalized = threading.Event()
		self._output_container: Optional[av.container.OutputContainer] = None
		self._out_stream: Optional[av.video.VideoStream] = None
		self._stream_lock = threading.Lock()
		self.stop_reason: Optional[str] = None
		self.dropped_units = 0
		self.written_packets = 0
		self.error: Optional[str] = None

	@property
	def is_finalized(self) -> bool:
		return self._finalized.is_set()

	@property
	def is_recording(self) -> bool:
		return self._finalized.is_set() is False

	def update_meta(self, source: str, payload: Any) -> None:
		# Meta item: (width, height, fps, codec name). The codec selects both
		# the demux format and the MP4 output track. The NALU worker emits meta
		# before any access unit, so the codec is normally resolved before the
		# first batch creates the track, and the lazy track creation follows it.
		# A codec change once the track exists is ignored for the duration of
		# this file (the next recording follows the new codec): the track and
		# the demux format both stay with the codec the track was created with.
		# Swapping in a second track could not work anyway: the packets remuxed
		# here always carry the raw demuxer's stream_index 0, so a track with a
		# higher index would never receive a packet, and PyAV would buffer every
		# later packet while waiting for that track's extradata.
		if source != self.source:
			return

		if isinstance(payload, tuple) and len(payload) == 4:
			width, height, meta_fps, codec_name = payload
			with self._stream_lock:
				if (
					isinstance(width, int)
					and isinstance(height, int)
					and width > 0
					and height > 0
					and self._out_stream is None
				):
					self._width = width
					self._height = height
				if isinstance(meta_fps, float) and meta_fps > 0:
					self._fps = meta_fps
				if (
					isinstance(codec_name, str)
					and codec_name in NALE_DEMUX_FORMAT_BY_CODEC
					and codec_name != self._codec_name
					and self._out_stream is None
				):
					self._codec_name = codec_name

	def push(self, payload: bytes, source: str) -> None:
		if source != self.source or self._finalized.is_set() is True:
			return

		try:
			self._au_queue.put_nowait(payload)
			return
		except queue.Full:
			pass

		# Drop the oldest unit so the live drain path never blocks.
		try:
			self._au_queue.get_nowait()
		except queue.Empty:
			pass

		try:
			self._au_queue.put_nowait(payload)
		except queue.Full:
			self.dropped_units += 1

	def start(self) -> None:
		threading.Thread(
			target=self._run, name=f"camera-recorder-{self.camera_id}", daemon=True
		).start()

	def stop(self) -> None:
		self._stop_event.set()

	def wait_for_finalization(self, timeout: float) -> bool:
		deadline = time.monotonic() + timeout
		while self._finalized.is_set() is False and time.monotonic() < deadline:
			time.sleep(0.02)

		return self._finalized.is_set()

	def status(self) -> dict[str, Any]:
		size_bytes = self.path.stat().st_size if self.path.exists() else 0

		return {
			"is_recording": self.is_recording,
			"source": self.source,
			"file_name": self.path.name,
			**_elapsed_status_fields(self.started_at),
			"size_bytes": size_bytes,
			"max_duration_s": self.max_duration_s,
			"stop_reason": self.stop_reason,
			"dropped_units": self.dropped_units,
			"written_packets": self.written_packets,
		}

	def _run(self) -> None:
		next_frame_index = 0

		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			# The container opens immediately, but the output track is created
			# lazily so it follows the codec resolved from update_meta.
			self._output_container = cast(
				av.container.OutputContainer,
				av.open(str(self.path), mode="w"),
			)
			saw_key_unit = False

			while True:
				duration_expired = (
					self.max_duration_s is not None
					and (time.monotonic() - self.started_at) >= self.max_duration_s
				)

				if self._stop_event.is_set() is True or duration_expired is True:
					self.stop_reason = "requested" if self._stop_event.is_set() else "max_duration"

					# Drain whatever is already queued so the tail is not lost.
					while True:
						try:
							unit = self._au_queue.get_nowait()
						except queue.Empty:
							break
						remuxed, next_frame_index = self._remux_batch(
							[unit], saw_key_unit, next_frame_index
						)
						# A keyless drain must stay keyless: the gate follows the
						# units actually seen, so the file finalizes empty instead of
						# opening with deltas that reference an IDR never recorded.
						saw_key_unit = saw_key_unit or _unit_has_key(unit, self._codec_name)
						self.written_packets += remuxed
					break

				try:
					unit = self._au_queue.get(timeout=0.25)
				except queue.Empty:
					continue

				units = [unit]
				# Batch the rest of the queue so one demux pass covers the burst.
				while True:
					try:
						units.append(self._au_queue.get_nowait())
					except queue.Empty:
						break

				remuxed, next_frame_index = self._remux_batch(
					units, saw_key_unit, next_frame_index
				)
				self.written_packets += remuxed
				saw_key_unit = saw_key_unit or _batch_has_key(units, self._codec_name)

			if self._output_container is not None:
				self._output_container.close()
				self._output_container = None
				logger.info(
					"camera_recording_finalized",
					camera_id=self.camera_id,
					path=str(self.path),
					packets=self.written_packets,
					dropped=self.dropped_units,
					stop_reason=self.stop_reason,
					size_bytes=self.path.stat().st_size if self.path.exists() else 0,
				)
		except Exception as exc:
			self.error = str(exc)
			logger.error(
				"camera_recording_failed",
				camera_id=self.camera_id,
				error=str(exc),
				error_type=type(exc).__name__,
			)
			if self._output_container is not None:
				try:
					self._output_container.close()
				except Exception:
					pass
		finally:
			self._finalized.set()

	def _ensure_output_stream(self) -> av.video.VideoStream:
		# Caller holds _stream_lock. The track name follows the codec:
		# "h264" for h264, "hevc" for hevc.
		if self._out_stream is not None:
			return self._out_stream

		if self._output_container is None:
			raise RuntimeError("recording output container is closed")

		self._out_stream = cast(
			av.video.VideoStream,
			self._output_container.add_stream(self._codec_name, rate=int(round(self._fps))),
		)
		# The raw demuxer leaves the muxer to infer dimensions from the SPS,
		# which it mis-parses for some HEVC streams (the container header
		# then reports the wrong resolution). Pin the stream meta's
		# dimensions explicitly so the header matches the bitstream.
		if self._width is not None:
			self._out_stream.width = self._width
		if self._height is not None:
			self._out_stream.height = self._height
		return self._out_stream

	def _remux_batch(
		self,
		units: list[bytes],
		saw_key_unit: bool,
		next_frame_index: int,
	) -> tuple[int, int]:
		# update_meta may run on the manager's thread, so serialize output
		# track creation with the remux path. The track is created only when a
		# batch actually passes the key gate.
		with self._stream_lock:
			if saw_key_unit is False:
				# The gate is per unit, not per batch: a batch may hold the key
				# anywhere, and the units ahead of it reference an IDR that never
				# reaches the file. Drop them so the mux starts at the key.
				first_key = _first_key_unit_index(units, self._codec_name)
				if first_key < 0:
					return 0, next_frame_index

				units = units[first_key:]

			out_stream = self._ensure_output_stream()
			return self._remux(units, out_stream, next_frame_index)

	def _remux(
		self,
		units: list[bytes],
		out_stream: av.video.VideoStream,
		next_frame_index: int,
	) -> tuple[int, int]:
		"""Remux one batch of access units into the open MP4.

		The key gate is applied per unit in _remux_batch, which trims every
		access unit ahead of the first key unit, so the file is decodable from
		its first frame. Returns (packets written, next frame index).
		"""
		if len(units) == 0 or out_stream is None:
			return 0, next_frame_index

		elementary = b"".join(units)
		demux_format = NALE_DEMUX_FORMAT_BY_CODEC.get(self._codec_name, "h264")
		container = cast(
			av.container.InputContainer,
			av.open(io.BytesIO(elementary), format=demux_format),
		)

		written = 0

		try:
			stream = container.streams.video[0]

			for packet in container.demux(stream):
				if packet.size == 0:
					continue

				# The raw h264/hevc demuxer leaves timestamps unset; assign a
				# monotonic frame index at the source rate so the MP4 muxer
				# has a consistent timeline even across reconnects. The raw
				# demuxer also leaves duration unset, so give each sample one
				# frame period: without it the MP4 muxer writes junk per-sample
				# durations and players hold the first frames for minutes.
				# dts stays None on purpose: B-frame sources need the muxer's
				# dts derivation, not a forced dts == pts.
				packet.pts = next_frame_index
				packet.dts = None
				packet.duration = 1
				packet.time_base = Fraction(1, int(round(self._fps)))
				if self._output_container is None:
					raise RuntimeError("recording output container is closed")

				self._output_container.mux_one(packet)
				written += 1
				next_frame_index += 1
		except av.error.InvalidDataError:
			# A structurally bad unit (RTSP loss, mid-stream reconnect) loses
			# only its own frames.
			pass
		finally:
			container.close()

		return written, next_frame_index


class MjpegDiskRecorder:
	"""Fallback recorder for cameras without a NALU pipe.

	Samples the manager's latest JPEG frame on an interval and re-encodes
	it into an MP4. Lower quality than the NALU remux path, but keeps the
	record toggle working for every enabled camera.
	"""

	SAMPLE_INTERVAL_S = 1.0 / 10

	def __init__(
		self,
		camera_id: str,
		path: Path,
		max_duration_s: Optional[int] = None,
		frame_fetch: Optional[Callable[[], Awaitable[Optional[bytes]]]] = None,
	) -> None:
		self.camera_id = camera_id
		self.path = path
		self.max_duration_s = max_duration_s
		self.started_at = time.monotonic()
		self._frame_fetch = frame_fetch
		self._stop_event = threading.Event()
		self._finalized = threading.Event()
		self._task: Optional[asyncio.Task] = None
		self.stop_reason: Optional[str] = None
		self.error: Optional[str] = None
		self.frames_written = 0

	@property
	def is_finalized(self) -> bool:
		return self._finalized.is_set()

	@property
	def is_recording(self) -> bool:
		return self._finalized.is_set() is False

	def start(self) -> None:
		self._task = asyncio.get_running_loop().create_task(self._run())

	def stop(self) -> None:
		self._stop_event.set()

	def cancel(self) -> None:
		if self._task is not None and self._task.cancelled() is False:
			self._task.cancel()

	def wait_for_finalization(self, timeout: float) -> bool:
		deadline = time.monotonic() + timeout
		while self._finalized.is_set() is False and time.monotonic() < deadline:
			time.sleep(0.02)

		return self._finalized.is_set()

	def status(self) -> dict[str, Any]:
		size_bytes = self.path.stat().st_size if self.path.exists() else 0

		return {
			"is_recording": self.is_recording,
			"source": "view",
			"file_name": self.path.name,
			**_elapsed_status_fields(self.started_at),
			"size_bytes": size_bytes,
			"max_duration_s": self.max_duration_s,
			"stop_reason": self.stop_reason,
			"dropped_units": 0,
			"written_packets": self.frames_written,
		}

	async def _run(self) -> None:
		writer: Optional[object] = None

		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)

			while self._stop_event.is_set() is False:
				duration_expired = (
					self.max_duration_s is not None
					and (time.monotonic() - self.started_at) >= self.max_duration_s
				)
				if duration_expired is True:
					self.stop_reason = "max_duration"
					break

				if self._frame_fetch is not None:
					try:
						frame_bytes = await self._frame_fetch()
					except Exception:
						frame_bytes = None

					if frame_bytes is not None:
						decoded = cv2.imdecode(
							np.frombuffer(frame_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
						)
						if decoded is not None:
							if writer is None:
								height, width = decoded.shape[:2]
								writer = cv2.VideoWriter(
									str(self.path),
									cast(Any, cv2).VideoWriter_fourcc(*"mp4v"),
									10.0,
									(width, height),
								)
								if writer is None or writer.isOpened() is False:
									raise RuntimeError("mp4v video encoder is unavailable")

							cast(Any, writer).write(decoded)
							self.frames_written += 1

				await asyncio.sleep(self.SAMPLE_INTERVAL_S)

			if self._stop_event.is_set() is True and self.stop_reason is None:
				self.stop_reason = "requested"
		except Exception as exc:
			self.error = str(exc)
			logger.error(
				"mjpeg_recording_failed",
				camera_id=self.camera_id,
				error=str(exc),
				error_type=type(exc).__name__,
			)
		finally:
			if writer is not None:
				cast(Any, writer).release()
			self._finalized.set()


def build_recording_file_name(camera_id: str, when: datetime | None = None) -> str:
	stamp = (when or datetime.now(timezone.utc)).strftime("%y%m%d%H%M%S")
	return f"{stamp}-{camera_id}.mp4"


_default_store: Optional[CameraRecordingStore] = None


def get_recording_store() -> CameraRecordingStore:
	global _default_store

	if _default_store is None:
		from openshelf_os.runtime_state import RuntimeStatePaths  # noqa: PLC0415

		_default_store = CameraRecordingStore(
			RuntimeStatePaths.current().data_dir / RECORDINGS_DIRNAME
		)

	return _default_store


class CameraRecordingStore:
	"""Filesystem layout and bookkeeping for recorded camera files."""

	def __init__(self, root: Path) -> None:
		self.root = root

	def camera_dir(self, camera_id: str) -> Path:
		return self.root / camera_id

	def new_file_path(self, camera_id: str, when: datetime | None = None) -> Path:
		"""Allocate a file name no existing recording already uses.

		Names resolve to one second, so a take started in the same second as a
		file already on disk would otherwise be opened over it and truncate it.
		The first take keeps the exact timestamp name; every later take in that
		second takes a -2, -3, ... suffix, which FILE_NAME_RE accepts so
		listing, download, and delete keep working for it.
		"""
		camera_dir = self.camera_dir(camera_id)
		base = Path(build_recording_file_name(camera_id, when))
		path = camera_dir / base.name
		counter = 2

		while path.exists():
			path = camera_dir / f"{base.stem}-{counter}{base.suffix}"
			counter += 1

		return path

	def validate_camera_id(self, camera_id: str) -> bool:
		return CAMERA_ID_RE.fullmatch(camera_id) is not None

	def validate_file_name(self, file_name: str) -> bool:
		return FILE_NAME_RE.fullmatch(file_name) is not None

	def resolve(self, camera_id: str, file_name: str) -> Path:
		# Both names are validated before callers reach here; re-check so the
		# store stays safe if a new caller forgets.
		if self.validate_camera_id(camera_id) is False or self.validate_file_name(file_name) is False:
			raise ValueError(f"invalid recording path: {camera_id}/{file_name}")

		match = re.match(
			r"^\d{12}-(?P<camera>[a-z0-9_]{1,32})(?:-\d+)?\.mp4$",
			file_name,
		)

		if match is None or match["camera"] != camera_id:
			raise ValueError(f"invalid recording path: {camera_id}/{file_name}")

		return self.camera_dir(camera_id) / file_name

	def delete(self, camera_id: str, file_name: str) -> None:
		path = self.resolve(camera_id, file_name)
		path.unlink()

	def list_recordings(
		self, active: Optional[dict[str, dict[str, Any]]] = None
	) -> list[dict[str, Any]]:
		active = active or {}
		entries: list[dict[str, Any]] = []

		if self.root.exists() is False:
			return entries

		for camera_dir in sorted(self.root.iterdir()):
			if camera_dir.is_dir() is False or self.validate_camera_id(camera_dir.name) is False:
				continue

			for file_path in sorted(camera_dir.iterdir(), key=lambda p: p.name, reverse=True):
				if file_path.is_file() is False or self.validate_file_name(file_path.name) is False:
					continue

				stat = file_path.stat()
				recording = active.get(camera_dir.name)
				is_recording = recording is not None and recording.get("file_name") == file_path.name

				entries.append({
					"camera_id": camera_dir.name,
					"file_name": file_path.name,
					"size_bytes": stat.st_size,
					"modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
					"is_recording": is_recording,
					"duration_s": None if is_recording else probe_video_duration_s(file_path),
				})

		return entries


def probe_video_duration_s(path: Path) -> Optional[float]:
	try:
		container = av.open(str(path))
	except Exception:
		return None

	try:
		stream = container.streams.video[0]
		if stream.duration is not None and stream.time_base is not None:
			return round(float(stream.duration * stream.time_base), 1)
		if container.duration is not None:
			return round(float(container.duration / 1_000_000), 1)
		return None
	finally:
		container.close()
