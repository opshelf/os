import asyncio
import io
import multiprocessing
import shutil
import threading
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import av
import cv2
import numpy as np
import pytest
from httpx import AsyncClient

from openshelf_os_server.core.camera import (
	CameraStreamManager,
	ToolCameraStreamManager,
	_au_contains_hevc_keyframe,
	_au_contains_idr,
	camera_registry,
)
from openshelf_os_server.core.camera_recording import (
	CameraDiskRecorder,
	CameraRecordingStore,
	MjpegDiskRecorder,
	build_recording_file_name,
)
from openshelf_os_server.routers.camera import _reap_self_stopped


def make_h264_units(
	n: int = 12, size: tuple[int, int] = (64, 64), keyint: int = 50
) -> list[bytes]:
	"""Encode synthetic frames into raw Annex-B H.264 access units.

	``keyint`` fixes the GOP length and disables scene cuts, so a test can
	place a key unit at a known index inside a batch.
	"""
	container = av.open(io.BytesIO(), mode="w", format="h264")
	stream = container.add_stream("libx264", rate=25)
	stream.width = size[0]
	stream.height = size[1]
	stream.pix_fmt = "yuv420p"
	stream.options = {
		"keyint": str(keyint),
		"min-keyint": str(keyint),
		"preset": "ultrafast",
		"x264-params": f"scenecut=0:keyint={keyint}:min-keyint={keyint}",
	}
	units: list[bytes] = []

	for i in range(n):
		arr = np.full((size[1], size[0], 3), (i * 20) % 256, dtype=np.uint8)
		frame = av.VideoFrame.from_ndarray(arr, format="bgr24").reformat(format="yuv420p")
		for packet in stream.encode(frame):
			units.append(bytes(packet))

	for packet in stream.encode(None):
		units.append(bytes(packet))

	container.close()
	return units


def make_hevc_units(n: int = 12, size: tuple[int, int] = (64, 64)) -> list[bytes]:
	"""Encode synthetic frames into raw Annex-B HEVC access units."""
	container = av.open(io.BytesIO(), mode="w", format="hevc")
	stream = container.add_stream("libx265", rate=25)
	stream.width = size[0]
	stream.height = size[1]
	stream.pix_fmt = "yuv420p"
	stream.options = {"keyint": "50", "preset": "ultrafast"}
	units: list[bytes] = []

	for i in range(n):
		arr = np.full((size[1], size[0], 3), (i * 20) % 256, dtype=np.uint8)
		frame = av.VideoFrame.from_ndarray(arr, format="bgr24").reformat(format="yuv420p")
		for packet in stream.encode(frame):
			units.append(bytes(packet))

	for packet in stream.encode(None):
		units.append(bytes(packet))

	container.close()
	return units


def open_video(path: Path) -> tuple[av.container.InputContainer, av.video.VideoStream]:
	container = av.open(str(path))
	stream = container.streams.video[0]
	return container, stream


def _is_key_unit(unit: bytes) -> bool:
	return _au_contains_idr(unit, False) or _au_contains_idr(unit, True)


def _is_hevc_key_unit(unit: bytes) -> bool:
	return _au_contains_hevc_keyframe(unit)


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
	"""Poll ``predicate`` until it is true, returning its last result."""
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline and predicate() is False:
		time.sleep(0.01)

	return predicate()


def _decoded_keyframe_flags(path: Path, limit: int = 4) -> list[bool]:
	"""is_keyframe per decoded sample, from a freshly opened container."""
	container, stream = open_video(path)
	flags: list[bool] = []

	try:
		for packet in container.demux(stream):
			for frame in stream.decode(packet):
				flags.append(bool(packet.is_keyframe))
				if len(flags) >= limit:
					return flags
	finally:
		container.close()

	return flags


def _wait_finalized(recorder: CameraDiskRecorder) -> None:
	assert recorder.wait_for_finalization(10.0) is True


def test_recorder_remuxes_units_into_decodable_mp4(tmp_path: Path) -> None:
	units = make_h264_units()
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.start()

	for unit in units:
		recorder.push(unit, "view")

	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.error is None
	assert recorder.path.exists() is True
	container, stream = open_video(recorder.path)

	assert stream.codec_context.name == "h264"
	packets = [p for p in container.demux(stream) if p.size > 0]
	assert len(packets) >= 12
	container.close()

	# A fresh container: the first pass consumed the stream.
	container, stream = open_video(recorder.path)
	decoded = 0
	for packet in container.demux(stream):
		for frame in stream.decode(packet):
			decoded += 1

	assert decoded >= 12
	container.close()

	# Sample durations must follow the frame rate: 12 frames @ 25 fps is
	# 0.48 s, so the reported container duration sits in a 20% band around
	# n_frames / rate (encoder padding allowed).
	rate = 25.0
	expected = len(units) / rate
	container = av.open(str(recorder.path))

	if container.duration is not None:
		duration_s = container.duration / 1_000_000
		assert 0.8 * expected <= duration_s <= 1.2 * expected
	else:
		# This av build lacks the format duration: the first 5 per-sample
		# durations must each be one frame period within 25%.
		stream = container.streams.video[0]
		for i, packet in enumerate(container.demux(stream)):
			if i >= 5:
				break
			assert packet.duration is not None
			assert abs(packet.duration * stream.time_base - 1.0 / rate) <= 0.25 / rate

	container.close()


def test_recorder_remuxes_hevc_units_into_decodable_mp4(tmp_path: Path) -> None:
	try:
		units = make_hevc_units()
	except Exception as exc:
		pytest.skip(f"libx265 is unavailable: {exc}")

	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path, codec_name="hevc")
	recorder.start()

	for unit in units:
		recorder.push(unit, "view")

	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.error is None
	assert recorder.path.exists() is True
	container, stream = open_video(recorder.path)

	# The output track follows the codec, not a hard-coded h264.
	assert stream.codec_context.name == "hevc"
	container.close()

	# A fresh container: the first pass consumed the stream.
	container, stream = open_video(recorder.path)
	decoded = 0
	for packet in container.demux(stream):
		for frame in stream.decode(packet):
			decoded += 1

	# libx265 may emit multiple packets per unit, so at least the key
	# frame must decode.
	assert decoded >= 1
	container.close()


def test_recorder_drops_units_before_first_key(tmp_path: Path) -> None:
	units = make_h264_units()
	# units[0] carries SPS/PPS/IDR; the rest are deltas until the next key.
	assert units[0] != units[1]
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.start()

	# Pre-key deltas must not open the file with an undecodable start.
	recorder.push(units[1], "view")
	recorder.push(units[2], "view")
	time.sleep(0.5)
	assert recorder.written_packets == 0

	for unit in units:
		recorder.push(unit, "view")

	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.written_packets >= 12
	container, stream = open_video(recorder.path)
	packets = [p for p in container.demux(stream) if p.size > 0]
	assert len(packets) >= 12
	container.close()


def test_remux_batch_drops_units_before_a_mid_batch_key(tmp_path: Path) -> None:
	# Deterministic GOP: keys at 0, 4 and 8, so a batch can start mid-GOP.
	units = make_h264_units(n=12, keyint=4)
	assert [i for i, unit in enumerate(units) if _is_key_unit(unit)] == [0, 4, 8]

	# The normal mid-GOP start: the key sits inside the batch, so the two
	# deltas ahead of it must never reach the muxer.
	batch = units[2:6]
	assert [_is_key_unit(unit) for unit in batch] == [False, False, True, False]

	path = tmp_path / "cam" / build_recording_file_name("cam")
	path.parent.mkdir(parents=True)
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder._output_container = av.open(str(path), mode="w")

	written, next_frame_index = recorder._remux_batch(batch, saw_key_unit=False, next_frame_index=0)
	assert recorder._output_container is not None
	recorder._output_container.close()

	# Only the key unit and the units behind it are muxed.
	assert written == len(batch) - 2
	assert next_frame_index == written

	container, stream = open_video(recorder.path)
	assert len([p for p in container.demux(stream) if p.size > 0]) == written
	container.close()

	flags = _decoded_keyframe_flags(recorder.path)
	assert flags
	assert flags[0] is True


def test_recorder_drops_units_before_a_key_inside_a_burst(tmp_path: Path) -> None:
	units = make_h264_units(n=12, keyint=4)

	# Queue the burst before the worker starts so it is remuxed as one
	# batch whose key sits at index 2.
	burst = units[2:6]
	assert [_is_key_unit(unit) for unit in burst] == [False, False, True, False]

	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	for unit in burst:
		recorder.push(unit, "view")

	recorder.start()
	time.sleep(0.3)
	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.error is None
	assert recorder.written_packets == len(burst) - 2

	flags = _decoded_keyframe_flags(recorder.path)
	assert flags
	assert flags[0] is True


def test_stop_drain_writes_nothing_without_a_key_unit(tmp_path: Path) -> None:
	# Every queued unit is a delta: a keyless recording must finalize empty
	# instead of opening the file with frames that reference a missing IDR.
	units = [unit for unit in make_h264_units(n=40, keyint=20) if _is_key_unit(unit) is False][:8]
	assert len(units) == 8

	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	for unit in units:
		recorder.push(unit, "view")

	recorder.start()
	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.error is None
	assert recorder.stop_reason == "requested"
	assert recorder.written_packets == 0
	# Nothing was muxed, so the container writes no moov and no file.
	assert recorder.path.exists() is False


def test_recorder_auto_stops_at_max_duration(tmp_path: Path) -> None:
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path, max_duration_s=1)
	recorder.start()

	assert recorder.wait_for_finalization(15.0) is True
	assert recorder.is_recording is False
	assert recorder.stop_reason == "max_duration"


def test_recorder_push_never_blocks_and_keeps_newest(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setattr(
		"openshelf_os_server.core.camera_recording.RECORDER_QUEUE_MAXSIZE", 3
	)
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)

	units = [b"\x00\x00\x00\x01\x41%s" % b"x" * i for i in range(8)]

	# No worker thread: push must stay non-blocking even past the maxsize.
	for unit in units:
		recorder.push(unit, "view")

	queued = list(recorder._au_queue.queue)

	assert len(queued) == 3
	assert queued == units[-3:]


def test_recorder_ignores_other_sources(tmp_path: Path) -> None:
	units = make_h264_units()
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "record", path)
	recorder.start()

	# The view source must not leak into a record-source recording.
	for unit in units:
		recorder.push(unit, "view")
	time.sleep(0.3)
	assert recorder.written_packets == 0

	for unit in units:
		recorder.push(unit, "record")

	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.written_packets >= 12
	assert recorder.path.stat().st_size > 0


def test_recorder_update_meta_switches_codec(tmp_path: Path) -> None:
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.update_meta("view", (1920, 1080, 30.0, "hevc"))
	recorder.update_meta("record", (1920, 1080, 30.0, "hevc"))

	assert recorder._codec_name == "hevc"
	assert recorder._fps == 30.0


def test_update_meta_after_the_track_exists_keeps_the_file_codec(tmp_path: Path) -> None:
	# The track already exists and holds packets: a late codec change must be
	# ignored for this file. Flipping the demux format under the existing track
	# would mux hevc payloads into an h264 track.
	units = make_h264_units(n=12, keyint=4)
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.start()

	for unit in units[:4]:
		recorder.push(unit, "view")

	assert _wait_until(lambda: recorder.written_packets > 0) is True
	written_before = recorder.written_packets

	recorder.update_meta("view", (1920, 1080, 25.0, "hevc"))
	container = recorder._output_container

	# Neither the demux format nor the track set may change, or every later
	# packet would be buffered waiting for the new track's extradata.
	assert recorder._codec_name == "h264"
	assert container is not None
	assert len(container.streams.video) == 1

	for unit in units[4:]:
		recorder.push(unit, "view")

	recorder.stop()
	_wait_finalized(recorder)

	# Muxing kept working after the refused codec change.
	assert recorder.error is None
	assert recorder._codec_name == "h264"
	assert recorder.written_packets > written_before

	file_container, stream = open_video(recorder.path)
	assert len(file_container.streams.video) == 1
	assert stream.codec_context.name == "h264"
	assert len([p for p in file_container.demux(stream) if p.size > 0]) >= recorder.written_packets
	file_container.close()


def test_update_meta_never_adds_a_second_output_track(tmp_path: Path) -> None:
	# The window the old swap branch gated on: the track exists while no packet
	# has been counted yet. A second track would never receive a packet, since
	# remuxed packets always carry the raw demuxer's stream_index 0, so PyAV
	# would buffer every later packet for that track's extradata and write no
	# moov at all.
	path = tmp_path / "cam" / build_recording_file_name("cam")
	path.parent.mkdir(parents=True)
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder._output_container = av.open(str(path), mode="w")
	with recorder._stream_lock:
		recorder._ensure_output_stream()

	recorder.update_meta("view", (1920, 1080, 25.0, "hevc"))
	container = recorder._output_container

	assert recorder._codec_name == "h264"
	assert container is not None
	assert len(container.streams.video) == 1
	container.close()


def test_meta_before_the_first_unit_creates_the_hevc_track(tmp_path: Path) -> None:
	# The primary path the refusal above must not break: meta precedes the
	# units, so the lazily created track follows the hevc codec.
	try:
		units = make_hevc_units(n=4)
	except Exception as exc:
		pytest.skip(f"libx265 is unavailable: {exc}")

	assert _is_hevc_key_unit(units[0]) is True
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.update_meta("view", (1920, 1080, 25.0, "hevc"))
	recorder.start()

	recorder.push(units[0], "view")

	assert _wait_until(lambda: recorder.written_packets > 0) is True
	container = recorder._output_container
	assert container is not None
	assert len(container.streams.video) == 1

	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.error is None
	file_container, stream = open_video(recorder.path)
	assert len(file_container.streams.video) == 1
	assert stream.codec_context.name == "hevc"
	file_container.close()


def test_store_lists_and_deletes_files(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")
	file_name = build_recording_file_name("overview_two")
	path = store.new_file_path("overview_two")
	path.parent.mkdir(parents=True)
	path.write_bytes(b"video-bytes")

	entries = store.list_recordings()
	assert len(entries) == 1
	assert entries[0]["camera_id"] == "overview_two"
	assert entries[0]["file_name"] == file_name
	assert entries[0]["size_bytes"] == len(b"video-bytes")
	assert entries[0]["is_recording"] is False

	store.delete("overview_two", file_name)
	assert path.exists() is False


def test_store_ignores_invalid_names(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")
	camera_dir = tmp_path / "recordings" / "overview_two"
	camera_dir.mkdir(parents=True)
	(camera_dir / "notes.txt").write_text("x")
	(camera_dir / "260101000000-overview_two-2x.mp4").write_text("x")
	(camera_dir / "26010100000-overview_two.mp4").write_text("x")

	assert store.list_recordings() == []


def test_store_resolve_rejects_traversal(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")

	with pytest.raises(ValueError):
		store.resolve("../", "260101000000-cam.mp4")

	with pytest.raises(ValueError):
		store.resolve("overview_two", "../../etc/passwd")


def test_store_marks_active_file(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")
	file_name = build_recording_file_name("tool")
	path = store.new_file_path("tool")
	path.parent.mkdir(parents=True)
	path.write_bytes(b"video-bytes")

	active = {
		"tool": {
			"is_recording": True,
			"file_name": file_name,
			"source": "view",
		}
	}
	entries = store.list_recordings(active)
	assert entries[0]["is_recording"] is True
	assert entries[0]["duration_s"] is None


def test_store_allocates_unused_names_inside_the_same_second(tmp_path: Path) -> None:
	"""Same-second takes must not share a name: the file name has 1 s resolution."""
	store = CameraRecordingStore(tmp_path / "recordings")
	when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

	first = store.new_file_path("tool", when)
	assert first.name == "260102030405-tool.mp4"
	first.parent.mkdir(parents=True)
	first.write_bytes(b"take-one")

	second = store.new_file_path("tool", when)
	assert second.name == "260102030405-tool-2.mp4"
	second.write_bytes(b"take-two")

	assert store.new_file_path("tool", when).name == "260102030405-tool-3.mp4"
	# Other cameras and later seconds keep their own sequence.
	assert store.new_file_path("overview_two", when).name == "260102030405-overview_two.mp4"
	assert store.new_file_path("tool", when + timedelta(seconds=1)).name == "260102030406-tool.mp4"

	# resolve still gates the counter names, so download and delete reach them.
	assert store.resolve("tool", second.name) == second
	assert sorted(e["file_name"] for e in store.list_recordings()) == [
		"260102030405-tool-2.mp4",
		"260102030405-tool.mp4",
	]

	store.delete("tool", second.name)
	assert second.exists() is False
	assert first.exists() is True


def test_two_take_recordings_keep_both_files(tmp_path: Path) -> None:
	"""A STOP then RECORD inside one second must not truncate the first take."""
	store = CameraRecordingStore(tmp_path / "recordings")
	when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
	units = make_h264_units()
	takes: list[tuple[Path, int]] = []

	for _ in range(2):
		path = store.new_file_path("overview_two", when)
		recorder = CameraDiskRecorder("overview_two", "view", path)
		recorder.start()

		for unit in units:
			recorder.push(unit, "view")

		recorder.stop()
		_wait_finalized(recorder)
		assert recorder.error is None
		takes.append((path, recorder.written_packets))

	assert [path.name for path, _ in takes] == [
		"260102030405-overview_two.mp4",
		"260102030405-overview_two-2.mp4",
	]

	for path, written in takes:
		assert path.stat().st_size > 0
		container, stream = open_video(path)
		assert len([p for p in container.demux(stream) if p.size > 0]) == written
		container.close()

	listed = sorted(e["file_name"] for e in store.list_recordings())
	assert listed == sorted(path.name for path, _ in takes)


def test_manager_drain_forwards_units_to_recorder(tmp_path: Path) -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_queue = multiprocessing.Queue(maxsize=16)

	# Stay under the queue maxsize: put() blocks once the deque is full on
	# this platform, and the real reader thread only ever queues one unit ahead.
	units = make_h264_units(n=8)
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.start()
	manager._disk_recorder = recorder

	now = time.monotonic()
	manager.nalu_queue.put(("view", "meta", 0.0, 0.0, (704, 576, 25.0, "h264")))

	for unit in units:
		manager.nalu_queue.put(("view", "au", 0.0, now, unit))

	deadline = time.monotonic() + 5.0

	while time.monotonic() < deadline and recorder.written_packets == 0:
		manager._drain_nalu_queue()

	recorder.stop()
	_wait_finalized(recorder)

	assert recorder.written_packets >= 8
	assert len(manager.nalu_view_ring) == len(units)


@pytest.mark.anyio
async def test_manager_stop_recording_without_recorder_raises() -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")

	with pytest.raises(ValueError):
		await manager.stop_recording()


@pytest.mark.anyio
async def test_tool_manager_recording_lifecycle(tmp_path: Path) -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_process.pid = 12345
	manager.nalu_queue = multiprocessing.Queue(maxsize=32)

	units = make_h264_units(n=8)

	with patch.object(ToolCameraStreamManager, "_start_nalu_worker", lambda self: None):
		await manager.start_recording("overview_two", "view")

	assert manager.recording_status() is not None
	assert manager.recording_status()["is_recording"] is True

	# A second start while recording is a conflict.
	with pytest.raises(ValueError):
		await manager.start_recording("overview_two", "view")

	# A request for the record source without a recording stream is rejected.
	manager.nalu_queue.put(("view", "meta", 0.0, 0.0, (704, 576, 25.0, "h264")))
	for unit in units:
		manager.nalu_queue.put(("view", "au", 0.0, time.monotonic(), unit))

	deadline = time.monotonic() + 5.0

	while time.monotonic() < deadline and (manager.recording_status() or {}).get(
		"written_packets", 0
	) == 0:
		manager._drain_nalu_queue()

	await manager.stop_recording()

	assert manager.recording_status() is None

	# The manager stop path finalises a recorder left behind.
	with patch.object(ToolCameraStreamManager, "_start_nalu_worker", lambda self: None):
		await manager.start_recording("overview_two", "view")

	assert manager.recording_status() is not None

	with patch.object(ToolCameraStreamManager, "_start_nalu_worker", lambda self: None):
		await manager.stop()


@pytest.mark.anyio
async def test_concurrent_stop_recording_finalizes_once(tmp_path: Path) -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_process.pid = 12345
	manager.nalu_queue = multiprocessing.Queue(maxsize=32)

	store = CameraRecordingStore(tmp_path / "recordings")
	with (
		patch.object(ToolCameraStreamManager, "_start_nalu_worker", lambda self: None),
		patch(
			"openshelf_os_server.core.camera_recording.get_recording_store",
			return_value=store,
		),
	):
		await manager.start_recording("overview_two", "view")

	recorder = manager._disk_recorder
	assert recorder is not None

	release = threading.Event()
	entered = threading.Event()
	stop_calls = 0
	finalize_calls = 0
	original_stop = recorder.stop

	def counting_stop() -> None:
		nonlocal stop_calls
		stop_calls += 1
		original_stop()

	def blocking_wait(_timeout: float) -> bool:
		nonlocal finalize_calls
		finalize_calls += 1
		entered.set()
		release.wait()
		return True

	recorder.stop = counting_stop
	recorder.wait_for_finalization = blocking_wait

	first = asyncio.create_task(manager.stop_recording())
	second = asyncio.create_task(manager.stop_recording())
	# Wait until the first stop is inside its finalization await so the
	# second enters during that window (deterministic interleave).
	deadline = time.monotonic() + 2.0
	while entered.is_set() is False and time.monotonic() < deadline:
		await asyncio.sleep(0.005)
	release.set()
	first_result, second_result = await asyncio.gather(
		first, second, return_exceptions=True
	)

	# Exactly one caller stops and finalizes the recorder; the other
	# observes the cleared recorder and raises, so the background lease
	# can only be released by the winner.
	assert first_result is None
	assert isinstance(second_result, ValueError)
	assert stop_calls == 1
	assert finalize_calls == 1

	await manager.stop()


@pytest.mark.anyio
async def test_concurrent_delete_releases_lease_once_with_real_manager(
	client: AsyncClient, tmp_path: Path,
) -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_process.pid = 12345
	manager.nalu_queue = multiprocessing.Queue(maxsize=32)

	store = CameraRecordingStore(tmp_path / "recordings")
	with (
		patch.object(ToolCameraStreamManager, "_start_nalu_worker", lambda self: None),
		patch(
			"openshelf_os_server.core.camera_recording.get_recording_store",
			return_value=store,
		),
	):
		await manager.start_recording("overview_two", "view")

	recorder = manager._disk_recorder
	assert recorder is not None

	# Hold the background lease, as the POST /record endpoint does.
	manager.increment_background_consumers()

	release = threading.Event()
	entered = threading.Event()
	stop_calls = 0
	finalize_calls = 0
	original_stop = recorder.stop

	def counting_stop() -> None:
		nonlocal stop_calls
		stop_calls += 1
		original_stop()

	def blocking_wait(_timeout: float) -> bool:
		nonlocal finalize_calls
		finalize_calls += 1
		entered.set()
		release.wait()
		return True

	recorder.stop = counting_stop
	recorder.wait_for_finalization = blocking_wait

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch(
			"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
			new_callable=AsyncMock,
		) as stop_stream,
	):
		first = asyncio.create_task(
			client.delete("/api/camera/overview_two/record")
		)
		second = asyncio.create_task(
			client.delete("/api/camera/overview_two/record")
		)
		# Wait until one stop is inside its finalization await so the other
		# DELETE observes the cleared recorder, then release it.
		deadline = time.monotonic() + 2.0
		while entered.is_set() is False and time.monotonic() < deadline:
			await asyncio.sleep(0.005)
		release.set()
		first, second = await asyncio.gather(first, second)

	# One DELETE wins (200) and releases the lease exactly once; the loser
	# sees the cleared recorder (409) and must not release it again.
	assert {first.status_code, second.status_code} == {200, 409}
	stop_stream.assert_awaited_once_with("overview_two", background=True)
	assert stop_calls == 1
	assert finalize_calls == 1

	await manager.stop()


def test_store_file_name_format() -> None:
	store = CameraRecordingStore(Path("unused"))
	name = build_recording_file_name("cam", None)

	assert name.endswith("-cam.mp4")

	for camera_id in ("cam", "overview_two"):
		file_name = build_recording_file_name(camera_id)
		assert store.validate_file_name(file_name) is True
		# The counter suffix a same-second take is allocated stays valid.
		assert store.validate_file_name(file_name.replace(".mp4", "-2.mp4")) is True

	for malformed in (
		"260101000000-cam-.mp4",
		"260101000000-cam-2x.mp4",
		"260101000000-cam-2.txt",
		"26010100000-cam-2.mp4",
		"260101000000-cam-2.MP4",
		"../260101000000-cam-2.mp4",
	):
		assert store.validate_file_name(malformed) is False


def _make_recording_manager_mock() -> MagicMock:
	manager = MagicMock(spec=ToolCameraStreamManager)
	manager.is_running = True
	manager.consumer_count = 0
	manager.nalu_consumer_count = 0
	manager.background_consumer_count = 0
	manager.latest_frame = None
	manager.has_frame = MagicMock(return_value=False)
	manager.is_available = AsyncMock(return_value=True)
	manager.recording_url = "rtsp://record"
	manager.start_recording = AsyncMock()
	manager.stop_recording = AsyncMock()
	manager.recording_status = MagicMock(return_value=None)
	return manager


_DiskUsage = namedtuple("_DiskUsage", "total used free")


def _pin_free_space(monkeypatch: pytest.MonkeyPatch, free_bytes: int) -> None:
	"""Pin the endpoint's shutil.disk_usage check to a chosen free value."""
	total = 64 * 1024**3
	monkeypatch.setattr(
		shutil,
		"disk_usage",
		lambda _path: _DiskUsage(total, total - free_bytes, free_bytes),
	)


@pytest.fixture(autouse=True)
def ample_free_space(monkeypatch: pytest.MonkeyPatch) -> None:
	# Recording tests must not depend on the real data volume's free space.
	_pin_free_space(monkeypatch, 8 * 1024**3)


@pytest.mark.anyio
async def test_start_and_stop_recording_lifecycle(client: AsyncClient) -> None:
	status_during = {
		"is_recording": True,
		"source": "record",
		"file_name": "260101000000-overview_two.mp4",
		"started_at": "2026-01-01T00:00:00+00:00",
		"elapsed_s": 1.0,
		"size_bytes": 10,
		"max_duration_s": None,
		"dropped_units": 0,
		"written_packets": 3,
	}
	state = {"recording": False}

	def start_recording(*_args, **_kwargs) -> None:
		if state["recording"] is True:
			raise ValueError("Camera is already recording")

		state["recording"] = True

	def recording_status() -> dict | None:
		if state["recording"] is True:
			return status_during

		return None

	manager = _make_recording_manager_mock()
	manager.start_recording = AsyncMock(side_effect=start_recording)
	manager.stop_recording = AsyncMock(side_effect=lambda: state.update(recording=False))
	manager.recording_status = MagicMock(side_effect=recording_status)

	with patch.dict(camera_registry, {"overview_two": manager}, clear=True):
		started = await client.post("/api/camera/overview_two/record")
		assert started.status_code == 200
		data = started.json()
		assert data["is_recording"] is True
		assert data["source"] == "record"

		manager.start_recording.assert_awaited_once()

		status = await client.get("/api/camera/overview_two/record")
		assert status.status_code == 200
		assert status.json()["file_name"] == "260101000000-overview_two.mp4"

		again = await client.post("/api/camera/overview_two/record")
		assert again.status_code == 409

		stopped = await client.delete("/api/camera/overview_two/record")
		assert stopped.status_code == 200
		assert stopped.json()["is_recording"] is False
		assert state["recording"] is False
		manager.stop_recording.assert_awaited_once()

		again_stopped = await client.delete("/api/camera/overview_two/record")
		assert again_stopped.status_code == 409


@pytest.mark.anyio
async def test_start_recording_unknown_camera(client: AsyncClient) -> None:
	with patch.dict(camera_registry, {}, clear=True):
		response = await client.post("/api/camera/missing/record")

	assert response.status_code == 404


@pytest.mark.anyio
async def test_start_recording_invalid_source(client: AsyncClient) -> None:
	manager = _make_recording_manager_mock()

	with patch.dict(camera_registry, {"overview_two": manager}, clear=True):
		response = await client.post(
			"/api/camera/overview_two/record", json={"source": "bogus"}
		)

	assert response.status_code == 400


@pytest.mark.anyio
async def test_start_recording_without_recording_stream_falls_back_to_view(
	client: AsyncClient,
) -> None:
	manager = _make_recording_manager_mock()
	manager.recording_url = ""
	manager.recording_status = MagicMock(
		return_value={"is_recording": True, "source": "view", "file_name": "f.mp4"}
	)

	with patch.dict(camera_registry, {"overview_two": manager}, clear=True):
		started = await client.post("/api/camera/overview_two/record")

	assert started.status_code == 200
	args, kwargs = manager.start_recording.await_args
	assert args[1] == "view"


@pytest.mark.anyio
async def test_start_recording_already_recording_conflict(client: AsyncClient) -> None:
	manager = _make_recording_manager_mock()
	manager.start_recording = AsyncMock(side_effect=ValueError("Camera is already recording"))

	with patch.dict(camera_registry, {"overview_two": manager}, clear=True):
		response = await client.post("/api/camera/overview_two/record")

	assert response.status_code == 409


@pytest.mark.anyio
async def test_start_recording_defaults_max_duration_to_one_hour(
	client: AsyncClient, tmp_path: Path
) -> None:
	# The demo page sends no max_duration_s; the recorder must still carry
	# the model's bound so no recording can grow past the volume.
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_queue = multiprocessing.Queue(maxsize=32)

	store = CameraRecordingStore(tmp_path / "recordings")
	with (
		patch.object(ToolCameraStreamManager, "_start_nalu_worker", lambda self: None),
		patch(
			"openshelf_os_server.core.camera_recording.get_recording_store",
			return_value=store,
		),
		patch(
			"openshelf_os_server.routers.camera.start_camera_stream_by_id",
			new_callable=AsyncMock,
		),
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
	):
		started = await client.post("/api/camera/overview_two/record")

		assert started.status_code == 200
		assert started.json()["max_duration_s"] == 3600
		recorder = manager._disk_recorder
		assert recorder is not None
		assert recorder.max_duration_s == 3600

	await manager.stop()


@pytest.mark.anyio
async def test_start_recording_refused_below_free_space_floor(
	client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
	# A forgotten RECORD must not fill the volume the database lives on:
	# below the floor the POST is refused before any lease is taken.
	manager = _make_recording_manager_mock()
	_pin_free_space(monkeypatch, 1024**3 - 1)

	start_lease = AsyncMock()
	release_lease = AsyncMock()
	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch("openshelf_os_server.routers.camera.start_camera_stream_by_id", start_lease),
		patch("openshelf_os_server.routers.camera.stop_camera_stream_by_id", release_lease),
	):
		response = await client.post("/api/camera/overview_two/record")

	assert response.status_code == 503
	assert response.json()["detail"] == "insufficient disk space for recording"
	start_lease.assert_not_awaited()
	release_lease.assert_not_awaited()
	manager.start_recording.assert_not_awaited()


@pytest.mark.anyio
async def test_list_recordings_uses_store(
	client: AsyncClient, tmp_path: Path
) -> None:
	manager = _make_recording_manager_mock()
	file_name = build_recording_file_name("overview_two")
	path = tmp_path / "recordings" / "overview_two" / file_name
	path.parent.mkdir(parents=True)
	path.write_bytes(b"video-bytes")

	manager.recording_status = MagicMock(
		return_value={"is_recording": True, "source": "view", "file_name": file_name}
	)

	store = CameraRecordingStore(tmp_path / "recordings")

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch(
			"openshelf_os_server.routers.camera.get_recording_store",
			return_value=store,
		),
	):
		response = await client.get("/api/camera/recordings")

	assert response.status_code == 200
	recording = response.json()["recordings"][0]
	assert recording["camera_id"] == "overview_two"
	assert recording["file_name"] == file_name
	assert recording["is_recording"] is True


@pytest.mark.anyio
async def test_list_recordings_survives_registry_pop_during_reap(
	client: AsyncClient, tmp_path: Path
) -> None:
	"""A camera unregistering mid-reap must not break the polled listing.

	unregister_camera_manager pops from the live registry (sizer teardown,
	camera config reload), so iterating that dict across an await raises
	"dictionary changed size during iteration" and 500s the endpoint the demo
	 page polls every 2 s.
	"""
	file_name = build_recording_file_name("cam")
	store = CameraRecordingStore(tmp_path / "recordings")

	for camera_id in ("tool", "overview_two"):
		path = store.root / camera_id / build_recording_file_name(camera_id)
		path.parent.mkdir(parents=True)
		path.write_bytes(b"video-bytes")

	# The first camera's reap unregisters that camera, so the pop lands inside
	# the listing loop; the second camera must still be reached and listed.
	tearing_down = _make_recording_manager_mock()
	tearing_down.recording_status = MagicMock(
		return_value={
			"is_recording": False,
			"source": "view",
			"file_name": build_recording_file_name("tool"),
		}
	)
	tearing_down.stop_recording = AsyncMock(
		side_effect=lambda *a, **k: camera_registry.pop("tool", None)
	)

	recording = _make_recording_manager_mock()
	recording.recording_status = MagicMock(
		return_value={
			"is_recording": True,
			"source": "view",
			"file_name": build_recording_file_name("overview_two"),
		}
	)

	with (
		patch.dict(
			camera_registry,
			{"tool": tearing_down, "overview_two": recording},
			clear=True,
		),
		patch(
			"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
			new_callable=AsyncMock,
		),
		patch(
			"openshelf_os_server.routers.camera.get_recording_store",
			return_value=store,
		),
	):
		response = await client.get("/api/camera/recordings")

	assert response.status_code == 200
	# The simulated unregister really ran inside the loop body.
	assert "tool" not in camera_registry
	assert {r["camera_id"]: r["is_recording"] for r in response.json()["recordings"]} == {
		"tool": False,
		"overview_two": True,
	}


@pytest.mark.anyio
async def test_download_recording(client: AsyncClient, tmp_path: Path) -> None:
	file_name = build_recording_file_name("tool")
	path = tmp_path / "recordings" / "tool" / file_name
	path.parent.mkdir(parents=True)
	path.write_bytes(b"video-bytes")
	store = CameraRecordingStore(tmp_path / "recordings")

	with (
		patch("openshelf_os_server.routers.camera.get_recording_store", return_value=store),
	):
		ok = await client.get(f"/api/camera/recordings/tool/{file_name}/download")
		assert ok.status_code == 200
		assert ok.content == b"video-bytes"

		missing = await client.get(
			f"/api/camera/recordings/tool/"
			f"{build_recording_file_name('tool', datetime.now(timezone.utc) + timedelta(seconds=1))}/download"
		)
		assert missing.status_code == 404

		bad = await client.get("/api/camera/recordings/..%2f..%2fetc/passwd/download")
		assert bad.status_code in (400, 404)


@pytest.mark.anyio
async def test_delete_recording(client: AsyncClient, tmp_path: Path) -> None:
	file_name = build_recording_file_name("tool")
	path = tmp_path / "recordings" / "tool" / file_name
	path.parent.mkdir(parents=True)
	path.write_bytes(b"video-bytes")
	store = CameraRecordingStore(tmp_path / "recordings")
	manager = _make_recording_manager_mock()

	with (
		patch.dict(camera_registry, {"tool": manager}, clear=True),
		patch("openshelf_os_server.routers.camera.get_recording_store", return_value=store),
	):
		manager.recording_status = MagicMock(
			return_value={
				"is_recording": True,
				"source": "view",
				"file_name": file_name,
			}
		)
		conflict = await client.delete(f"/api/camera/recordings/tool/{file_name}")
		assert conflict.status_code == 409

		manager.recording_status = MagicMock(return_value=None)
		deleted = await client.delete(f"/api/camera/recordings/tool/{file_name}")
		assert deleted.status_code == 200
		assert path.exists() is False

		missing = await client.delete(f"/api/camera/recordings/tool/{file_name}")
		assert missing.status_code == 404


@pytest.mark.anyio
async def test_status_poll_releases_stale_lease_after_auto_stop(
	client: AsyncClient,
) -> None:
	manager = _make_recording_manager_mock()
	state = {"phase": "recording"}

	finalized_status: dict = {
		"is_recording": False,
		"source": "record",
		"file_name": "260101000000-overview_two.mp4",
		"started_at": "2026-01-01T00:00:00+00:00",
		"elapsed_s": 30.0,
		"size_bytes": 10,
		"max_duration_s": 30,
		"dropped_units": 0,
		"written_packets": 3,
		"stop_reason": "max_duration",
	}

	def recording_status() -> dict | None:
		if state["phase"] == "recording":
			return dict(finalized_status, is_recording=True, stop_reason=None)
		if state["phase"] == "finalized":
			return dict(finalized_status)
		return None

	manager.recording_status = MagicMock(side_effect=recording_status)
	manager.stop_recording = AsyncMock(side_effect=lambda *a, **k: state.update(phase="cleared"))

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch(
			"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
			new_callable=AsyncMock,
		) as stop_stream,
	):
		started = await client.post(
			"/api/camera/overview_two/record", json={"max_duration_s": 30}
		)
		assert started.status_code == 200

		# The recorder self-stops at max_duration; its background lease is
		# still held, so the stream would never stop on its own.
		state["phase"] = "finalized"

		status = await client.get("/api/camera/overview_two/record")
		assert status.status_code == 200
		assert status.json()["is_recording"] is False
		manager.stop_recording.assert_awaited_once()
		stop_stream.assert_awaited_once_with("overview_two", background=True)

		# A second poll no-ops: the recorder is cleared and the lease is
		# already released, so it must not be released a second time.
		again = await client.get("/api/camera/overview_two/record")
		assert again.status_code == 200
		assert again.json()["is_recording"] is False
		stop_stream.assert_awaited_once()


@pytest.mark.anyio
async def test_status_poll_releases_stale_lease_after_recorder_crash(
	client: AsyncClient,
) -> None:
	manager = _make_recording_manager_mock()
	state = {"phase": "recording"}

	crashed_status: dict = {
		"is_recording": False,
		"source": "record",
		"file_name": "260101000000-overview_two.mp4",
		"started_at": "2026-01-01T00:00:00+00:00",
		"elapsed_s": 30.0,
		"size_bytes": 10,
		"max_duration_s": None,
		"dropped_units": 0,
		"written_packets": 3,
		"stop_reason": None,
		"error": "No space left on device",
	}

	def recording_status() -> dict | None:
		if state["phase"] == "recording":
			return dict(crashed_status, is_recording=True)
		if state["phase"] == "finalized":
			return dict(crashed_status)
		return None

	manager.recording_status = MagicMock(side_effect=recording_status)
	manager.stop_recording = AsyncMock(side_effect=lambda *a, **k: state.update(phase="cleared"))

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch(
			"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
			new_callable=AsyncMock,
		) as stop_stream,
	):
		started = await client.post("/api/camera/overview_two/record")
		assert started.status_code == 200

		# The recorder thread died on an exception (disk full, read-only
		# data dir): stop_reason stays None, but the background lease
		# must still be released, or the NALU worker runs for the
		# process lifetime.
		state["phase"] = "finalized"

		status = await client.get("/api/camera/overview_two/record")
		assert status.status_code == 200
		assert status.json()["is_recording"] is False
		manager.stop_recording.assert_awaited_once()
		stop_stream.assert_awaited_once_with("overview_two", background=True)

		# A second poll no-ops: the recorder is cleared and the lease is
		# already released, so it must not be released a second time.
		again = await client.get("/api/camera/overview_two/record")
		assert again.status_code == 200
		assert again.json()["is_recording"] is False
		stop_stream.assert_awaited_once()


@pytest.mark.anyio
async def test_reap_skips_release_when_concurrent_stop_cleared_recorder() -> None:
	manager = _make_recording_manager_mock()
	manager.recording_status = MagicMock(
		return_value={
			"is_recording": False,
			"source": "record",
			"file_name": "260101000000-overview_two.mp4",
			"stop_reason": None,
		}
	)
	manager.stop_recording = AsyncMock(side_effect=ValueError("Camera is not recording"))

	with patch(
		"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
		new_callable=AsyncMock,
	) as stop_stream:
		await _reap_self_stopped("overview_two", manager)

	# The concurrent clear already owns the release; a second one could
	# stop a newer recording.
	stop_stream.assert_not_awaited()


@pytest.mark.anyio
async def test_status_poll_releases_stale_lease_after_explicit_stop(
	client: AsyncClient,
) -> None:
	manager = _make_recording_manager_mock()
	state = {"phase": "stopped"}

	stopped_status: dict = {
		"is_recording": False,
		"source": "record",
		"file_name": "260101000000-overview_two.mp4",
		"started_at": "2026-01-01T00:00:00+00:00",
		"elapsed_s": 10.0,
		"size_bytes": 10,
		"max_duration_s": None,
		"dropped_units": 0,
		"written_packets": 3,
		"stop_reason": "requested",
	}

	def recording_status() -> dict | None:
		if state["phase"] == "stopped":
			return dict(stopped_status)
		return None

	manager.recording_status = MagicMock(side_effect=recording_status)
	manager.stop_recording = AsyncMock(side_effect=lambda *a, **k: state.update(phase="cleared"))

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch(
			"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
			new_callable=AsyncMock,
		) as stop_stream,
	):
		# Safety net for an explicit stop whose DELETE release never ran:
		# the poll of the attached finalised recorder releases the lease.
		status = await client.get("/api/camera/overview_two/record")
		assert status.status_code == 200
		assert status.json()["is_recording"] is False
		manager.stop_recording.assert_awaited_once()
		stop_stream.assert_awaited_once_with("overview_two", background=True)

		# The recorder is cleared, so the status is None and a later
		# poll no-ops without releasing again.
		again = await client.get("/api/camera/overview_two/record")
		assert again.status_code == 200
		assert again.json()["is_recording"] is False
		stop_stream.assert_awaited_once()


@pytest.mark.anyio
async def test_concurrent_delete_releases_lease_once(client: AsyncClient) -> None:
	manager = _make_recording_manager_mock()
	state = {"phase": "recording", "clears": 0}

	recording_status_dict: dict = {
		"is_recording": True,
		"source": "record",
		"file_name": "260101000000-overview_two.mp4",
		"started_at": "2026-01-01T00:00:00+00:00",
		"elapsed_s": 1.0,
		"size_bytes": 10,
		"max_duration_s": None,
		"dropped_units": 0,
		"written_packets": 3,
	}

	def recording_status() -> dict | None:
		if state["phase"] == "recording":
			return dict(recording_status_dict)
		return None

	async def stop_recording(*_args: Any, **_kwargs: Any) -> None:
		# Yield so the other DELETE reads the attached status before
		# this one clears the recorder.
		await asyncio.sleep(0)
		state["clears"] += 1
		if state["clears"] > 1:
			raise ValueError("Camera is not recording")
		state["phase"] = "cleared"

	manager.recording_status = MagicMock(side_effect=recording_status)
	manager.stop_recording = AsyncMock(side_effect=stop_recording)

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch(
			"openshelf_os_server.routers.camera.stop_camera_stream_by_id",
			new_callable=AsyncMock,
		) as stop_stream,
	):
		first, second = await asyncio.gather(
			client.delete("/api/camera/overview_two/record"),
			client.delete("/api/camera/overview_two/record"),
		)

	assert {first.status_code, second.status_code} == {200, 409}
	# Exactly one release: the loser cleared nothing, so it must not
	# release a lease it does not own.
	stop_stream.assert_awaited_once_with("overview_two", background=True)


@pytest.mark.anyio
async def test_start_recording_reaps_stale_auto_stop_before_new_lease(
	client: AsyncClient,
) -> None:
	manager = _make_recording_manager_mock()
	state = {"phase": "stale"}

	fresh_status: dict = {
		"is_recording": True,
		"source": "record",
		"file_name": "260101000000-overview_two.mp4",
		"started_at": "2026-01-01T00:00:00+00:00",
		"elapsed_s": 0.0,
		"size_bytes": 0,
		"max_duration_s": None,
		"dropped_units": 0,
		"written_packets": 0,
	}

	def recording_status() -> dict | None:
		if state["phase"] == "stale":
			return dict(fresh_status, is_recording=False, stop_reason="max_duration")
		if state["phase"] == "recording":
			return dict(fresh_status)
		return None

	manager.recording_status = MagicMock(side_effect=recording_status)
	manager.stop_recording = AsyncMock(side_effect=lambda *a, **k: state.update(phase="cleared"))
	manager.start_recording = AsyncMock(side_effect=lambda *a, **k: state.update(phase="recording"))

	order: list[str] = []
	start_lease = AsyncMock(side_effect=lambda *a, **k: order.append("start"))
	release_lease = AsyncMock(side_effect=lambda *a, **k: order.append("stop"))

	with (
		patch.dict(camera_registry, {"overview_two": manager}, clear=True),
		patch("openshelf_os_server.routers.camera.start_camera_stream_by_id", start_lease),
		patch("openshelf_os_server.routers.camera.stop_camera_stream_by_id", release_lease),
	):
		started = await client.post("/api/camera/overview_two/record")
		assert started.status_code == 200
		assert started.json()["is_recording"] is True

	# The stale auto-stopped lease is released before the new one is
	# taken, so the count does not grow on every cycle.
	assert order == ["stop", "start"]
	release_lease.assert_awaited_once_with("overview_two", background=True)
	manager.stop_recording.assert_awaited_once()


@pytest.mark.anyio
async def test_mjpeg_recorder_samples_frames_into_mp4(tmp_path: Path) -> None:
	def make_jpeg() -> bytes:
		arr = np.full((64, 64, 3), 100, dtype=np.uint8)

		success, buffer = cv2.imencode(".jpg", arr)
		assert success is True
		return buffer.tobytes()

	frames = iter([make_jpeg() for _ in range(100)])
	path = tmp_path / "cam" / build_recording_file_name("cam")
	recorder = MjpegDiskRecorder("cam", path, frame_fetch=lambda: _next_frame(frames))
	recorder.start()

	await _sleep(0.4)
	recorder.stop()
	await _wait_task(recorder)

	assert recorder.error is None
	assert recorder.path.exists() is True
	container, stream = open_video(recorder.path)
	packets = [p for p in container.demux(stream) if p.size > 0]
	assert len(packets) >= 2
	container.close()


async def _next_frame(frames: iter) -> bytes:
	return next(frames, b"")


async def _sleep(seconds: float) -> None:
	await asyncio.sleep(seconds)


async def _wait_task(recorder: MjpegDiskRecorder) -> None:
	task = recorder._task

	if task is not None:
		await asyncio.wait_for(task, timeout=10.0)


def _uvc_manager() -> CameraStreamManager:
	"""A real UVC manager with no capture loop running: only pacing is tested."""
	manager = CameraStreamManager(camera=MagicMock())
	manager.is_running = True
	return manager


def test_uvc_capture_paces_down_without_viewer_or_recording() -> None:
	manager = _uvc_manager()

	assert manager.consumer_count == 0
	assert manager._disk_recorder is None
	assert manager.effective_target_fps() == min(
		manager.target_fps, manager.background_target_fps
	)


@pytest.mark.anyio
async def test_active_recording_holds_uvc_capture_at_target_fps(tmp_path: Path) -> None:
	# A recording with no live viewer is a background consumer. Pacing it to
	# the idle rate would make MjpegDiskRecorder write mostly duplicate frames.
	manager = _uvc_manager()
	store = CameraRecordingStore(tmp_path / "recordings")

	with patch(
		"openshelf_os_server.core.camera_recording.get_recording_store",
		return_value=store,
	):
		await manager.start_recording("io", "view")

	recorder = manager._disk_recorder
	assert isinstance(recorder, MjpegDiskRecorder)
	assert manager.consumer_count == 0
	assert recorder.is_recording is True
	assert manager.effective_target_fps() == manager.target_fps

	# The polled status keeps the same recorder attached, so pacing holds.
	assert manager.recording_status()["is_recording"] is True
	assert manager.effective_target_fps() == manager.target_fps

	# Once stopped and finalised, the capture loop goes back to idle pacing.
	await manager.stop_recording()

	assert manager._disk_recorder is None
	assert recorder.is_recording is False
	assert manager.effective_target_fps() == min(
		manager.target_fps, manager.background_target_fps
	)


@pytest.mark.anyio
async def test_finalised_recorder_left_attached_restores_idle_pacing(
	tmp_path: Path,
) -> None:
	# The recorder self-stops at max_duration but stays attached until the
	# status poll reaps it: only an actively recording one may hold full rate.
	manager = _uvc_manager()
	store = CameraRecordingStore(tmp_path / "recordings")

	with patch(
		"openshelf_os_server.core.camera_recording.get_recording_store",
		return_value=store,
	):
		await manager.start_recording("io", "view", max_duration_s=0)

	recorder = manager._disk_recorder
	assert isinstance(recorder, MjpegDiskRecorder)
	# Finalization runs on the event loop, so wait for it off-loop.
	assert await asyncio.to_thread(recorder.wait_for_finalization, 10.0) is True

	assert manager._disk_recorder is recorder
	assert recorder.is_recording is False
	assert manager.effective_target_fps() == min(
		manager.target_fps, manager.background_target_fps
	)

	await manager.stop()
