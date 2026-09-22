import asyncio
import io
import multiprocessing
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import av
import numpy as np
import pytest
from httpx import AsyncClient

from openshelf_os_server.core.camera import (
	ToolCameraStreamManager,
	camera_registry,
)
from openshelf_os_server.core.camera_recording import (
	CameraDiskRecorder,
	CameraRecordingStore,
	MjpegDiskRecorder,
	build_recording_file_name,
)


def make_h264_units(n: int = 12, size: tuple[int, int] = (64, 64)) -> list[bytes]:
	"""Encode synthetic frames into raw Annex-B H.264 access units."""
	container = av.open(io.BytesIO(), mode="w", format="h264")
	stream = container.add_stream("libx264", rate=25)
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


def _wait_finalized(recorder: CameraDiskRecorder) -> None:
	assert recorder.wait_for_finalization(10.0) is True


def test_recorder_remuxes_units_into_decodable_mp4(tmp_path: Path) -> None:
	units = make_h264_units()
	path = tmp_path / "cam" / build_recording_file_name("view")
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


def test_recorder_drops_units_before_first_key(tmp_path: Path) -> None:
	units = make_h264_units()
	# units[0] carries SPS/PPS/IDR; the rest are deltas until the next key.
	assert units[0] != units[1]
	path = tmp_path / "cam" / build_recording_file_name("view")
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


def test_recorder_auto_stops_at_max_duration(tmp_path: Path) -> None:
	path = tmp_path / "cam" / build_recording_file_name("view")
	recorder = CameraDiskRecorder("cam", "view", path, max_duration_s=1)
	recorder.start()

	assert recorder.wait_for_finalization(15.0) is True
	assert recorder.is_recording is False
	assert recorder.stop_reason == "max_duration"


def test_recorder_push_never_blocks_and_keeps_newest(tmp_path: Path, monkeypatch) -> None:
	monkeypatch.setattr(
		"openshelf_os_server.core.camera_recording.RECORDER_QUEUE_MAXSIZE", 3
	)
	path = tmp_path / "cam" / build_recording_file_name("view")
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
	path = tmp_path / "cam" / build_recording_file_name("record")
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
	path = tmp_path / "cam" / build_recording_file_name("view")
	recorder = CameraDiskRecorder("cam", "view", path)
	recorder.update_meta("view", (1920, 1080, 30.0, "hevc"))
	recorder.update_meta("record", (1920, 1080, 30.0, "hevc"))

	assert recorder._codec_name == "hevc"
	assert recorder._fps == 30.0


def test_store_lists_and_deletes_files(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")
	file_name = build_recording_file_name("view")
	path = store.new_file_path("overview_two", "view")
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
	(camera_dir / "20260101T000000Z-weird.mp4").write_text("x")

	assert store.list_recordings() == []


def test_store_resolve_rejects_traversal(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")

	with pytest.raises(ValueError):
		store.resolve("../", "20260101T000000Z-view.mp4")

	with pytest.raises(ValueError):
		store.resolve("overview_two", "../../etc/passwd")


def test_store_marks_active_file(tmp_path: Path) -> None:
	store = CameraRecordingStore(tmp_path / "recordings")
	file_name = build_recording_file_name("view")
	path = store.new_file_path("tool", "view")
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


def test_manager_drain_forwards_units_to_recorder(tmp_path: Path) -> None:
	manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
	manager.nalu_process = MagicMock()
	manager.nalu_process.is_alive = MagicMock(return_value=True)
	manager.nalu_queue = multiprocessing.Queue(maxsize=16)

	# Stay under the queue maxsize: put() blocks once the deque is full on
	# this platform, and the real reader thread only ever queues one unit ahead.
	units = make_h264_units(n=8)
	path = tmp_path / "cam" / build_recording_file_name("view")
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


def test_store_file_name_format() -> None:
	name = build_recording_file_name("view", None)

	assert name.endswith("-view.mp4")

	for source in ("view", "record"):
		file_name = build_recording_file_name(source)
		assert CameraRecordingStore(Path("unused")).validate_file_name(file_name) is True


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


@pytest.mark.anyio
async def test_start_and_stop_recording_lifecycle(client: AsyncClient) -> None:
	status_during = {
		"is_recording": True,
		"source": "record",
		"file_name": "20260101T000000Z-record.mp4",
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
		assert status.json()["file_name"] == "20260101T000000Z-record.mp4"

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
async def test_list_recordings_uses_store(
	client: AsyncClient, tmp_path: Path
) -> None:
	manager = _make_recording_manager_mock()
	file_name = build_recording_file_name("view")
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
async def test_download_recording(client: AsyncClient, tmp_path: Path) -> None:
	file_name = build_recording_file_name("view")
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
			f"/api/camera/recordings/tool/{build_recording_file_name('record')}/download"
		)
		assert missing.status_code == 404

		bad = await client.get("/api/camera/recordings/..%2f..%2fetc/passwd/download")
		assert bad.status_code in (400, 404)


@pytest.mark.anyio
async def test_delete_recording(client: AsyncClient, tmp_path: Path) -> None:
	file_name = build_recording_file_name("view")
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
async def test_mjpeg_recorder_samples_frames_into_mp4(tmp_path: Path) -> None:
	def make_jpeg() -> bytes:
		arr = np.full((64, 64, 3), 100, dtype=np.uint8)
		import cv2

		success, buffer = cv2.imencode(".jpg", arr)
		assert success is True
		return buffer.tobytes()

	frames = iter([make_jpeg() for _ in range(100)])
	path = tmp_path / "cam" / build_recording_file_name("view")
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
	import asyncio

	await asyncio.sleep(seconds)


async def _wait_task(recorder: MjpegDiskRecorder) -> None:
	import asyncio

	task = recorder._task

	if task is not None:
		await asyncio.wait_for(task, timeout=10.0)
