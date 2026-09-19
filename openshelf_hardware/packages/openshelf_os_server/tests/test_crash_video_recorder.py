from __future__ import annotations

import asyncio
import io
import threading
import time
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest

from openshelf_os_server.core.camera import (
    _au_contains_hevc_keyframe,
    _au_contains_idr,
    _split_annexb_nalus,
    camera_registry,
)
from openshelf_os_server.core.crash_video_recorder import (
    MIB,
    CrashVideoRecorder,
    CrashVideoRecorderConfig,
    NaluWindow,
    RecordedCameraFrame,
)


class FakeCameraManager:
    def __init__(self) -> None:
        self.is_running = False
        self.consumer_count = 0
        self.background_consumer_count = 0
        self.latest_frame: bytes | None = None
        self.last_frame_at: float | None = None

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False

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

    async def get_latest_frame(self) -> bytes | None:
        return self.latest_frame


def recorder_config(**overrides) -> CrashVideoRecorderConfig:
    values = {
        "lookback_seconds": 15,
        "sample_fps": 3,
        "max_width": 640,
        "max_camera_buffer_bytes": 16 * MIB,
        "max_attachment_bytes": 4 * MIB,
        "max_total_attachment_bytes": 14 * MIB,
    }
    values.update(overrides)
    return CrashVideoRecorderConfig(**values)


@pytest.mark.parametrize(
    ("configured_fps", "expected_fps"),
    [(1, 1), (2, 2), (3, 3), (10, 3)],
)
def test_recorder_config_clamps_fps_to_supported_background_range(
    monkeypatch: pytest.MonkeyPatch, configured_fps: int, expected_fps: int
) -> None:
    monkeypatch.setenv("CRASH_RECORDER_FPS", str(configured_fps))

    assert CrashVideoRecorderConfig.from_env().sample_fps == expected_fps


@pytest.mark.parametrize(
    ("configured_width", "expected_width"),
    [(160, 160), (320, 320), (640, 640), (1920, 640)],
)
def test_recorder_config_clamps_width_to_pi_safe_range(
    monkeypatch: pytest.MonkeyPatch, configured_width: int, expected_width: int
) -> None:
    monkeypatch.setenv("CRASH_RECORDER_MAX_WIDTH", str(configured_width))

    assert CrashVideoRecorderConfig.from_env().max_width == expected_width


@pytest.mark.anyio
async def test_recorder_keeps_camera_running_without_live_stream_consumers() -> None:
    manager = FakeCameraManager()
    recorder = CrashVideoRecorder(recorder_config())

    with patch.dict(camera_registry, {"tool": manager}, clear=True):
        await recorder.start()
        assert manager.is_running is True
        assert manager.consumer_count == 0
        assert manager.background_consumer_count == 1

        manager.latest_frame = b"jpeg"
        manager.last_frame_at = 100.0
        await recorder._sample_once(100.0)

        assert [frame.data for frame in recorder._buffers["tool"]] == [b"jpeg"]

        await recorder.stop()

    assert manager.is_running is False
    assert manager.consumer_count == 0
    assert manager.background_consumer_count == 0


def test_recorder_prunes_old_frames_and_enforces_camera_memory_limit() -> None:
    recorder = CrashVideoRecorder(recorder_config(max_camera_buffer_bytes=7))

    recorder._append_frame("tool", RecordedCameraFrame(80.0, b"old"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(99.0, b"1234"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"5678"), now=100.0)

    frames = recorder._buffers["tool"]
    assert [(frame.captured_at, frame.data) for frame in frames] == [(100.0, b"5678")]
    assert recorder._buffer_bytes["tool"] == 4


@pytest.mark.anyio
async def test_reconcile_cameras_discards_unregistered_camera_frames() -> None:
    manager = FakeCameraManager()
    manager.is_running = True
    manager.background_consumer_count = 1
    recorder = CrashVideoRecorder(recorder_config())
    recorder._owned_managers["tool"] = manager
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"stale"), now=100.0)
    recorder._last_frame_at["tool"] = 100.0

    with patch.dict(camera_registry, {}, clear=True):
        assert recorder._freeze_buffers(100.0) == {}

        await recorder._reconcile_cameras()

    assert "tool" not in recorder._owned_managers
    assert "tool" not in recorder._buffers
    assert "tool" not in recorder._buffer_bytes
    assert "tool" not in recorder._last_frame_at
    assert manager.background_consumer_count == 0
    assert manager.is_running is False


@pytest.mark.anyio
async def test_reconcile_cameras_discards_frames_for_replaced_camera_id() -> None:
    original_manager = FakeCameraManager()
    original_manager.is_running = True
    original_manager.background_consumer_count = 1
    replacement_manager = FakeCameraManager()
    recorder = CrashVideoRecorder(recorder_config())
    recorder._owned_managers["tool"] = original_manager
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"old"), now=100.0)
    recorder._last_frame_at["tool"] = 100.0

    with patch.dict(camera_registry, {"tool": replacement_manager}, clear=True):
        assert recorder._freeze_buffers(100.0) == {}

        await recorder._reconcile_cameras()

        assert original_manager.background_consumer_count == 0
        assert original_manager.is_running is False

    assert recorder._owned_managers["tool"] is replacement_manager
    assert list(recorder._buffers["tool"]) == []
    assert recorder._buffer_bytes["tool"] == 0
    assert "tool" not in recorder._last_frame_at


@pytest.mark.anyio
async def test_stop_releases_unregistered_manager_lease() -> None:
    manager = FakeCameraManager()
    manager.is_running = True
    manager.background_consumer_count = 1
    recorder = CrashVideoRecorder(recorder_config())
    recorder._owned_managers["tool"] = manager

    with patch.dict(camera_registry, {}, clear=True):
        await recorder.stop()

    assert manager.background_consumer_count == 0
    assert manager.is_running is False


@pytest.mark.anyio
async def test_capture_returns_one_attachment_per_camera() -> None:
    recorder = CrashVideoRecorder(recorder_config())
    recorder._running = True
    for camera_id in ("tool", "overview_one"):
        recorder._append_frame(camera_id, RecordedCameraFrame(90.0, b"one"), now=100.0)
        recorder._append_frame(camera_id, RecordedCameraFrame(100.0, b"two"), now=100.0)

    with patch.object(
        recorder,
        "_encode_camera",
        new=AsyncMock(side_effect=[b"tool-video", b"overview-video"]),
    ):
        with patch(
            "openshelf_os_server.core.crash_video_recorder.time.monotonic", return_value=100.0
        ):
            artifacts = await recorder.capture("simulated motor fault")

    assert len(artifacts.attachments) == 2
    assert {
        attachment.filename.split("-motion-crash-")[0] for attachment in artifacts.attachments
    } == {
        "tool",
        "overview_one",
    }
    assert all(attachment.content_type == "video/webm" for attachment in artifacts.attachments)
    assert artifacts.context["attachment_count"] == 2
    assert artifacts.context["cameras"]["tool"]["attached"] is True
    assert artifacts.context["cameras"]["overview_one"]["attached"] is True


@pytest.mark.anyio
async def test_capture_respects_total_attachment_budget() -> None:
    recorder = CrashVideoRecorder(recorder_config(max_total_attachment_bytes=10))
    recorder._running = True
    for camera_id in ("tool", "overhead"):
        recorder._append_frame(camera_id, RecordedCameraFrame(90.0, b"one"), now=100.0)
        recorder._append_frame(camera_id, RecordedCameraFrame(100.0, b"two"), now=100.0)

    encoder = AsyncMock(return_value=b"123456")
    with patch.object(recorder, "_encode_camera", new=encoder):
        with patch(
            "openshelf_os_server.core.crash_video_recorder.time.monotonic", return_value=100.0
        ):
            artifacts = await recorder.capture("fault")

    assert len(artifacts.attachments) == 1
    assert artifacts.context["cameras"]["tool"]["attached"] is True
    assert artifacts.context["cameras"]["overhead"]["error"] == "total_attachment_budget_exceeded"
    assert encoder.await_args_list[0].kwargs["max_attachment_bytes"] == 10
    assert encoder.await_args_list[1].kwargs["max_attachment_bytes"] == 4


@pytest.mark.anyio
async def test_capture_freezes_second_crash_before_waiting_for_encoder() -> None:
    recorder = CrashVideoRecorder(recorder_config())
    recorder._running = True
    recorder._append_frame("tool", RecordedCameraFrame(90.0, b"before-a"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"at-a"), now=100.0)
    first_encode_started = asyncio.Event()
    release_first_encode = asyncio.Event()
    encoded_frame_data: list[list[bytes]] = []

    async def encode_camera(
        _camera_id: str,
        frames: list[RecordedCameraFrame],
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        del max_attachment_bytes
        encoded_frame_data.append([frame.data for frame in frames])
        if len(encoded_frame_data) == 1:
            first_encode_started.set()
            await release_first_encode.wait()
        return b"video"

    crash_times = iter(
        [
            datetime(1970, 1, 1, 0, 1, 40, tzinfo=timezone.utc),
            datetime(1970, 1, 1, 0, 1, 50, tzinfo=timezone.utc),
        ]
    )

    class FakeDatetime:
        @classmethod
        def now(cls, _tz: timezone) -> datetime:
            return next(crash_times)

    crash_monotonic_times = iter([100.0, 110.0])

    class FakeTime:
        @classmethod
        def monotonic(cls) -> float:
            return next(crash_monotonic_times)

    with patch.object(recorder, "_encode_camera", new=encode_camera):
        with patch("openshelf_os_server.core.crash_video_recorder.time", FakeTime):
            with patch(
                "openshelf_os_server.core.crash_video_recorder.datetime", FakeDatetime
            ):
                first_capture = asyncio.create_task(recorder.capture("first crash"))
                await first_encode_started.wait()

                recorder._append_frame(
                    "tool", RecordedCameraFrame(105.0, b"before-b"), now=105.0
                )
                second_capture = asyncio.create_task(recorder.capture("second crash"))
                await asyncio.sleep(0)
                assert recorder._pending_captures == 2

                recorder._append_frame(
                    "tool", RecordedCameraFrame(111.0, b"after-b"), now=111.0
                )
                release_first_encode.set()
                await first_capture
                second_artifacts = await second_capture

    assert encoded_frame_data == [
        [b"before-a", b"at-a"],
        [b"at-a", b"before-b"],
    ]
    assert second_artifacts.context["crash_at"] == "1970-01-01T00:01:50+00:00"


@pytest.mark.anyio
async def test_begin_capture_freezes_window_and_timestamp_before_later_frames() -> None:
    recorder = CrashVideoRecorder(recorder_config())
    recorder._running = True
    recorder._append_frame("tool", RecordedCameraFrame(90.0, b"before"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"at-fault"), now=100.0)
    encoded_frames: list[list[bytes]] = []

    async def encode_camera(
        _camera_id: str,
        frames: list[RecordedCameraFrame],
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        del max_attachment_bytes
        encoded_frames.append([frame.data for frame in frames])
        return b"video"

    fault_time = datetime(1970, 1, 1, 0, 1, 40, tzinfo=timezone.utc)
    with patch.object(recorder, "_encode_camera", new=encode_camera):
        with patch(
            "openshelf_os_server.core.crash_video_recorder.time.monotonic", return_value=100.0
        ):
            with patch(
                "openshelf_os_server.core.crash_video_recorder.datetime"
            ) as mocked_datetime:
                mocked_datetime.now.return_value = fault_time
                prepared = recorder.begin_capture("fault")

        recorder._append_frame("tool", RecordedCameraFrame(110.0, b"after"), now=110.0)
        artifacts = await prepared

    assert encoded_frames == [[b"before", b"at-fault"]]
    assert artifacts.context["crash_at"] == "1970-01-01T00:01:40+00:00"
    assert recorder._pending_captures == 0
    assert prepared._frozen == {}


@pytest.mark.anyio
async def test_cancelled_unstarted_begin_capture_releases_its_pending_slot() -> None:
    recorder = CrashVideoRecorder(recorder_config())
    recorder._running = True
    recorder._append_frame("tool", RecordedCameraFrame(99.0, b"one"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"two"), now=100.0)

    with patch(
        "openshelf_os_server.core.crash_video_recorder.time.monotonic", return_value=100.0
    ):
        prepared = recorder.begin_capture("fault")

    assert recorder._pending_captures == 1
    prepared.cancel()
    assert recorder._pending_captures == 0
    assert prepared._frozen == {}
    with pytest.raises(asyncio.CancelledError):
        await prepared


@pytest.mark.anyio
async def test_cancelled_started_capture_keeps_pending_slot_until_encoding_finishes() -> None:
    recorder = CrashVideoRecorder(recorder_config())
    recorder._running = True
    recorder._append_frame("tool", RecordedCameraFrame(99.0, b"one"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"two"), now=100.0)
    first_encode_started = asyncio.Event()
    release_first_encode = asyncio.Event()

    async def encode_camera(
        _camera_id: str,
        _frames: list[RecordedCameraFrame],
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        del max_attachment_bytes
        first_encode_started.set()
        await release_first_encode.wait()
        return b"video"

    crash_monotonic_times = iter([100.0, 101.0])

    class FakeTime:
        @classmethod
        def monotonic(cls) -> float:
            return next(crash_monotonic_times)

    with patch.object(recorder, "_encode_camera", new=encode_camera):
        with patch("openshelf_os_server.core.crash_video_recorder.time", FakeTime):
            first_capture = asyncio.create_task(recorder.capture("first crash"))
            await first_encode_started.wait()
            queued_capture = asyncio.create_task(recorder.capture("queued crash"))
            await asyncio.sleep(0)

            assert recorder._pending_captures == 2
            saturated_artifacts = await recorder.capture("dropped crash")
            assert saturated_artifacts.attachments == []
            assert saturated_artifacts.context["status"] == "queue_saturated"
            assert saturated_artifacts.context["reason"] == "dropped crash"

            queued_capture.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued_capture
            assert recorder._pending_captures == 2

            release_first_encode.set()
            await first_capture
            await asyncio.sleep(0)

    assert recorder._pending_captures == 0


@pytest.mark.anyio
async def test_cancelled_capture_holds_encoder_lock_and_pending_slot_until_thread_finishes() -> None:
    recorder = CrashVideoRecorder(recorder_config())
    recorder._running = True
    recorder._append_frame("tool", RecordedCameraFrame(99.0, b"one"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"two"), now=100.0)
    first_encoder_started = threading.Event()
    first_encoder_released = threading.Event()
    second_encoder_started = threading.Event()
    encoder_order: list[str] = []

    def encode_camera_sync(
        _camera_id: str,
        _frames: list[RecordedCameraFrame],
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        del max_attachment_bytes
        if first_encoder_started.is_set() is False:
            encoder_order.append("first_started")
            first_encoder_started.set()
            first_encoder_released.wait()
            encoder_order.append("first_finished")
        else:
            assert encoder_order == ["first_started", "first_finished"]
            encoder_order.append("second_started")
            second_encoder_started.set()
        return b"video"

    crash_monotonic_times = iter([100.0, 101.0])

    class FakeTime:
        @classmethod
        def monotonic(cls) -> float:
            return next(crash_monotonic_times)

    with patch.object(recorder, "_encode_camera_sync", new=encode_camera_sync):
        with patch("openshelf_os_server.core.crash_video_recorder.time", FakeTime):
            first_prepared = recorder.begin_capture("first crash")
            first_capture = asyncio.ensure_future(first_prepared)
            await asyncio.wait_for(asyncio.to_thread(first_encoder_started.wait), timeout=1)

            first_capture.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_capture
            assert recorder._pending_captures == 1
            assert first_prepared._frozen

            second_capture = asyncio.create_task(recorder.capture("second crash"))
            await asyncio.sleep(0)
            assert recorder._pending_captures == 2
            assert second_encoder_started.is_set() is False

            first_encoder_released.set()
            await asyncio.wait_for(asyncio.to_thread(second_encoder_started.wait), timeout=1)
            assert first_prepared._frozen == {}
            await second_capture

    assert encoder_order == ["first_started", "first_finished", "second_started"]
    assert recorder._pending_captures == 0


def test_opencv_encoder_writes_each_decoded_frame_before_decoding_next() -> None:
    recorder = CrashVideoRecorder(recorder_config(max_width=4))
    frames = [RecordedCameraFrame(float(index), b"jpeg") for index in range(3)]
    pending_frame_ids: set[int] = set()
    write_count = 0

    def decode_frame(*_args: object) -> np.ndarray:
        assert pending_frame_ids == set()
        decoded = np.zeros((4, 4, 3), dtype=np.uint8)
        pending_frame_ids.add(id(decoded))
        return decoded

    class FakeVideoWriter:
        def __init__(self, output_name: str, *_args: object) -> None:
            self.output_path = Path(output_name)

        def isOpened(self) -> bool:
            return True

        def write(self, frame: np.ndarray) -> None:
            nonlocal write_count
            assert id(frame) in pending_frame_ids
            pending_frame_ids.remove(id(frame))
            write_count += 1

        def release(self) -> None:
            self.output_path.write_bytes(b"video")

    with patch.object(cv2, "imdecode", side_effect=decode_frame):
        with patch.object(cv2, "VideoWriter", FakeVideoWriter):
            video = recorder._encode_camera_sync("tool", frames)

    assert video == b"video"
    assert write_count == 3
    assert pending_frame_ids == set()


def test_opencv_encoder_rejects_oversized_temp_file_before_reading_it() -> None:
    recorder = CrashVideoRecorder(recorder_config(max_width=4))
    frames = [RecordedCameraFrame(float(index), b"jpeg") for index in range(2)]

    class FakeVideoWriter:
        def __init__(self, output_name: str, *_args: object) -> None:
            self.output_path = Path(output_name)

        def isOpened(self) -> bool:
            return True

        def write(self, _frame: np.ndarray) -> None:
            pass

        def release(self) -> None:
            self.output_path.write_bytes(b"too-large")

    with patch.object(
        cv2,
        "imdecode",
        side_effect=[
            np.zeros((4, 4, 3), dtype=np.uint8),
            np.zeros((4, 4, 3), dtype=np.uint8),
        ],
    ):
        with patch.object(cv2, "VideoWriter", FakeVideoWriter):
            with patch.object(Path, "read_bytes", side_effect=AssertionError):
                with pytest.raises(ValueError, match="size budget"):
                    recorder._encode_camera_sync(
                        "tool", frames, max_attachment_bytes=4
                    )


@pytest.mark.anyio
async def test_opencv_encoder_produces_browser_compatible_webm() -> None:
    recorder = CrashVideoRecorder(recorder_config(max_width=64))
    frames: list[RecordedCameraFrame] = []
    for index in range(6):
        image = np.full((48, 64, 3), index * 30, dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", image)
        assert ok is True
        frames.append(RecordedCameraFrame(float(index), encoded.tobytes()))

    video = await recorder._encode_camera("tool", frames)

    assert video.startswith(b"\x1aE\xdf\xa3")
    assert len(video) > 100

# --- NALU-window (RTSP) recording path -----------------------------------


def _make_rtsp_manager(
    window: list[tuple[float, bytes]],
    *,
    extradata: bytes = b"",
    codec_name: str = "h264",
) -> "ToolCameraStreamManager":
    """Real ToolCameraStreamManager with a pre-filled NALU ring.

    start/stop are stubbed so the recorder never spawns the NALU worker
    process. When extradata is given, the manager recognizes the ring's
    Annex-B AU wire form, like a real RTSP stream with SPS/PPS meta.
    """
    from unittest.mock import AsyncMock

    from openshelf_os_server.core.camera import ToolCameraStreamManager

    manager = ToolCameraStreamManager(rtsp_url="rtsp://camera")
    now = time.monotonic()
    manager.nalu_view_ring.extend((pts, data, now) for pts, data in window)
    manager.nalu_view_codec = codec_name
    if extradata:
        manager.nalu_extradata = extradata
        manager.nalu_meta = (160, 120, 25.0, codec_name)
    manager.start = AsyncMock()
    manager.stop = AsyncMock()
    manager.is_running = True
    return manager


def _make_h264_window(frame_count: int = 10, width: int = 160, height: int = 120):
    """Real Annex-B H.264 access units at 25 fps.

    Returns the (pts, unit) window and the stream's Annex-B SPS/PPS
    extradata for the manager's codec meta.
    """
    import av

    import numpy as np

    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="h264")
    stream = container.add_stream("libx264", rate=Fraction(25, 1))
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"

    for index in range(frame_count):
        image = np.full((height, width, 3), (index * 20) % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    buffer.seek(0)
    elementary = buffer.read()

    probe = av.open(io.BytesIO(elementary), format="h264")
    probe_stream = probe.streams.video[0]
    for _probe_frame in probe_stream.decode():
        break
    extradata = bytes(probe_stream.codec_context.extradata or b"")
    probe.close()

    markers = [
        offset
        for offset in range(len(elementary) - 3)
        if elementary[offset:offset + 4] == b"\x00\x00\x00\x01"
    ]
    units: list[bytes] = []
    for index, start in enumerate(markers):
        end = markers[index + 1] if index + 1 < len(markers) else len(elementary)
        units.append(elementary[start:end])

    return ([(index * 0.04, unit) for index, unit in enumerate(units)], extradata)


def _drop_through_first_idr(units: list[tuple[float, bytes]]) -> list[tuple[float, bytes]]:
    """Window with everything up to and including the first IDR removed."""
    from openshelf_os_server.core.camera import _au_contains_idr

    for index, (_pts, unit) in enumerate(units):
        if _au_contains_idr(unit, au_is_avcc=False):
            return units[index + 1 :]
    raise AssertionError("window contains no IDR")


def _make_hevc_window(frame_count: int = 10, width: int = 160, height: int = 120):
    """Real Annex-B HEVC access units at 25 fps, every frame an IDR.

    g=1 forces a keyframe on every frame so the stream carries IDR
    NALs early, like the h264 helper's stream. Returns the (pts, unit)
    window, one demuxed access unit each, and the stream's Annex-B
    VPS/SPS/PPS extradata.
    """
    import av

    import numpy as np

    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="hevc")
    stream = container.add_stream("libx265", rate=Fraction(25, 1))
    stream.codec_context.options = {"g": "1"}
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"

    for index in range(frame_count):
        image = np.full((height, width, 3), (index * 20) % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    buffer.seek(0)
    elementary = buffer.read()

    probe = av.open(io.BytesIO(elementary), format="hevc")
    probe_stream = probe.streams.video[0]
    # The raw demuxer already surfaces the parameter sets once seen.
    extradata = bytes(probe_stream.codec_context.extradata or b"")
    units: list[bytes] = []
    for packet in probe.demux(probe_stream):
        data = bytes(packet)
        if data:
            units.append(data)
    probe.close()

    return ([(index * 0.04, unit) for index, unit in enumerate(units)], extradata)


def _make_open_gop_hevc_window(frame_count: int = 40, gop: int = 10, width: int = 160, height: int = 120):
    """Open-GOP Annex-B HEVC access units, like an RTSP camera stream.

    Without g=1, libx265 emits one IRAP NAL type for the first key
    frame and a different one for every later key frame (20 vs 21 in
    this build). Returns the (pts, unit) window with everything up to
    and including the first key-frame AU dropped, the way a rolling
    capture window looks once the stream's start has scrolled out of
    it, plus the stream's Annex-B VPS/SPS/PPS extradata.
    """
    import av

    import numpy as np

    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="hevc")
    stream = container.add_stream("libx265", rate=Fraction(25, 1))
    stream.codec_context.options = {"g": str(gop)}
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"

    for index in range(frame_count):
        image = np.full((height, width, 3), (index * 20) % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode(None):
        container.mux(packet)
    container.close()
    buffer.seek(0)
    elementary = buffer.read()

    probe = av.open(io.BytesIO(elementary), format="hevc")
    probe_stream = probe.streams.video[0]
    extradata = bytes(probe_stream.codec_context.extradata or b"")
    units: list[bytes] = []
    for packet in probe.demux(probe_stream):
        data = bytes(packet)
        if data:
            units.append(data)
    probe.close()

    first_key_frame = None
    for index, unit in enumerate(units):
        if _au_contains_hevc_keyframe(unit):
            first_key_frame = index
            break

    if first_key_frame is None or first_key_frame + 1 >= len(units):
        raise AssertionError("open-GOP window holds no access units past its first key frame")

    units = units[first_key_frame + 1 :]

    return ([(index * 0.04, unit) for index, unit in enumerate(units)], extradata)


@pytest.mark.anyio
async def test_capture_freezes_nalu_window_for_rtsp_cameras() -> None:
    window = [(0.0, b"au-one"), (0.04, b"au-two"), (0.08, b"au-three")]
    rtsp_manager = _make_rtsp_manager(window)
    uvc_manager = FakeCameraManager()

    recorder = CrashVideoRecorder(recorder_config())

    with patch.dict(
        camera_registry,
        {"tool": rtsp_manager, "iobox_sizer": uvc_manager},
        clear=True,
    ):
        await recorder.start()
        uvc_manager.latest_frame = b"jpeg"
        uvc_manager.last_frame_at = 100.0
        await recorder._sample_once(100.0)

        frozen = recorder._freeze_buffers(100.0)
        await recorder.stop()

    assert isinstance(frozen["tool"], NaluWindow)
    assert frozen["tool"].frames == tuple(window)
    assert [frame.data for frame in frozen["iobox_sizer"]] == [b"jpeg"]


@pytest.mark.anyio
async def test_capture_freezes_skips_short_nalu_windows() -> None:
    rtsp_manager = _make_rtsp_manager([(0.0, b"au-one")])
    recorder = CrashVideoRecorder(recorder_config())

    with patch.dict(camera_registry, {"tool": rtsp_manager}, clear=True):
        await recorder.start()

        frozen = recorder._freeze_buffers(100.0)
        await recorder.stop()

    assert frozen == {}


@pytest.mark.anyio
async def test_nalu_window_encode_produces_browser_compatible_webm() -> None:
    units, _extradata = _make_h264_window()
    window = NaluWindow(frames=tuple(units))
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    video = await recorder._encode_nalu_window("tool", window)

    assert video.startswith(b"\x1aE\xdf\xa3")
    assert len(video) > 500


@pytest.mark.anyio
async def test_capture_records_keyframe_error_for_window_without_idr() -> None:
    units, extradata = _make_h264_window(frame_count=30)
    units = _drop_through_first_idr(units)
    assert len(units) >= 2
    rtsp_manager = _make_rtsp_manager(units, extradata=extradata)
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with patch.dict(camera_registry, {"tool": rtsp_manager}, clear=True):
        await recorder.start()
        artifacts = await recorder.capture("simulated motor fault")
        await recorder.stop()

    assert artifacts.attachments == []
    details = artifacts.context["cameras"]["tool"]
    assert details["attached"] is False
    assert "key frame" in details["error"]
    assert "InvalidData" not in details["error"]


@pytest.mark.anyio
async def test_capture_attaches_idr_window_before_extradata_is_known() -> None:
    # Right after a worker (re)connect, the ring already holds Annex-B
    # AUs but nalu_extradata has not surfaced yet, so the codec meta
    # is unknown. The frozen window must still be labeled Annex-B:
    # the first AU carries its IDR behind an AUD prefix, which the
    # AVCC misparse of the old label reports as a missing key frame.
    units, _extradata = _make_h264_window(frame_count=3)
    window = [
        (0.0, units[0][1] + units[1][1]),  # SPS + PPS + AUD + IDR, one AU
        (0.04, units[2][1]),  # first P-slice AU
    ]
    assert _au_contains_idr(window[0][1], au_is_avcc=True) is False
    rtsp_manager = _make_rtsp_manager(window)
    assert rtsp_manager.nalu_extradata is None
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with patch.dict(camera_registry, {"tool": rtsp_manager}, clear=True):
        await recorder.start()
        artifacts = await recorder.capture("simulated motor fault")
        await recorder.stop()

    details = artifacts.context["cameras"]["tool"]
    assert details["attached"] is True
    assert len(artifacts.attachments) == 1
    assert artifacts.attachments[0].data.startswith(b"\x1aE\xdf\xa3")


@pytest.mark.anyio
async def test_capture_survives_corrupted_slice_in_nalu_window() -> None:
    units, extradata = _make_h264_window(frame_count=30)
    # Flip the first mid-window P-slice's NAL type byte to IDR (5), a
    # structurally invalid AU that the decoder rejects outright.
    for index, (pts, unit) in enumerate(units):
        if unit[4] & 0x1F != 1:
            continue
        corrupted = bytearray(unit)
        corrupted[4] = (corrupted[4] & 0xE0) | 5
        units[index] = (pts, bytes(corrupted))
        break
    rtsp_manager = _make_rtsp_manager(units, extradata=extradata)
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with patch.dict(camera_registry, {"tool": rtsp_manager}, clear=True):
        await recorder.start()
        artifacts = await recorder.capture("simulated motor fault")
        await recorder.stop()

    details = artifacts.context["cameras"]["tool"]
    assert details["attached"] is True
    assert len(artifacts.attachments) == 1
    assert artifacts.attachments[0].data.startswith(b"\x1aE\xdf\xa3")


@pytest.mark.anyio
async def test_capture_attaches_real_hevc_nalu_window() -> None:
    units, extradata = _make_hevc_window()
    assert len(units) >= 2
    assert any(_au_contains_hevc_keyframe(unit) for _pts, unit in units)
    rtsp_manager = _make_rtsp_manager(units, extradata=extradata, codec_name="hevc")
    # HEVC extradata must not synthesize a WebCodecs meta: the WS
    # header stays unknown and the fan-out answers 4503, as for every
    # non-H.264 stream.
    assert rtsp_manager.nalu_codec_meta() is None
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with patch.dict(camera_registry, {"tool": rtsp_manager}, clear=True):
        await recorder.start()
        frozen = recorder._freeze_buffers(time.monotonic())
        assert frozen["tool"].codec_name == "hevc"
        artifacts = await recorder.capture("simulated motor fault")
        await recorder.stop()

    details = artifacts.context["cameras"]["tool"]
    assert details["attached"] is True
    assert len(artifacts.attachments) == 1
    assert artifacts.attachments[0].data.startswith(b"\x1aE\xdf\xa3")


@pytest.mark.anyio
async def test_capture_attaches_open_gop_hevc_window_past_first_key_frame() -> None:
    units, extradata = _make_open_gop_hevc_window()
    assert len(units) >= 2
    assert any(_au_contains_hevc_keyframe(unit) for _pts, unit in units)
    # Regression signature: the window's key frames are IRAP NALs the
    # old gate (IDR 19/20 only) rejected, so it must still attach
    # with no type-19/20 NAL anywhere in the window.
    for _pts, unit in units:
        nal_types = {
            (nal_unit[0] >> 1) & 0x3F
            for nal_unit in _split_annexb_nalus(unit)
            if nal_unit
        }
        assert not nal_types & {19, 20}

    rtsp_manager = _make_rtsp_manager(units, extradata=extradata, codec_name="hevc")
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with patch.dict(camera_registry, {"tool": rtsp_manager}, clear=True):
        await recorder.start()
        frozen = recorder._freeze_buffers(time.monotonic())
        assert frozen["tool"].codec_name == "hevc"
        artifacts = await recorder.capture("simulated motor fault")
        await recorder.stop()

    details = artifacts.context["cameras"]["tool"]
    assert details["attached"] is True
    assert len(artifacts.attachments) == 1
    assert artifacts.attachments[0].data.startswith(b"\x1aE\xdf\xa3")


def test_au_contains_hevc_keyframe_truth_table() -> None:
    # The HEVC NAL unit type is the top six bits of the first byte.
    # Every IRAP type (16-21) starts a random access point; encoders
    # use different ones for the first key frame than for later ones,
    # so the gate must accept the whole range.
    bla_w_lp = b"\x00\x00\x00\x01" + bytes([16 << 1]) + b"bla-w-lp"
    bla_w_radl = b"\x00\x00\x00\x01" + bytes([17 << 1]) + b"bla-w-radl"
    bla_n_lp = b"\x00\x00\x00\x01" + bytes([18 << 1]) + b"bla-n-lp"
    idr_w_radl = b"\x00\x00\x00\x01" + bytes([19 << 1]) + b"idr-w-radl"
    idr_n_lp = b"\x00\x00\x00\x01" + bytes([20 << 1]) + b"idr-n-lp"
    cra_nut = b"\x00\x00\x00\x01" + bytes([21 << 1]) + b"cra-nut"

    for irap in (bla_w_lp, bla_w_radl, bla_n_lp, idr_w_radl, idr_n_lp, cra_nut):
        assert _au_contains_hevc_keyframe(irap) is True

    # Non-IRAP NAL types are not key frames, including the neighbors
    # of the IRAP range.
    rsv_vcl_15 = b"\x00\x00\x00\x01" + bytes([15 << 1]) + b"rsv-vcl-15"
    rsv_vcl_22 = b"\x00\x00\x00\x01" + bytes([22 << 1]) + b"rsv-vcl-22"
    trail_r = b"\x00\x00\x00\x01" + b"\x02" + b"trail-r"
    sps = b"\x00\x00\x00\x01" + b"\x42" + b"sps"
    pps = b"\x00\x00\x00\x01" + b"\x44" + b"pps"

    for non_irap in (rsv_vcl_15, rsv_vcl_22, trail_r, sps, pps):
        assert _au_contains_hevc_keyframe(non_irap) is False
    assert _au_contains_hevc_keyframe(b"") is False

    # A parameter-set-led access unit whose slice NAL is the CRA.
    assert _au_contains_hevc_keyframe(sps + pps + cra_nut) is True


def test_nalu_window_unsupported_codec_reports_explicit_error() -> None:
    units, _extradata = _make_h264_window(frame_count=4)
    window = NaluWindow(frames=tuple(units), codec_name="mjpeg")
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with pytest.raises(ValueError, match="unsupported NALU codec") as excinfo:
        recorder._encode_nalu_window_sync("tool", window)

    assert "InvalidData" not in str(excinfo.value)


def test_nalu_window_hevc_without_idr_reports_keyframe_error() -> None:
    window = NaluWindow(
        frames=(
            (0.0, b"\x00\x00\x00\x01" + b"\x00" + b"trail-r-slice"),
            (0.04, b"\x00\x00\x00\x01" + b"\x02" + b"trail-n-slice"),
        ),
        codec_name="hevc",
    )
    recorder = CrashVideoRecorder(recorder_config(max_width=160))

    with pytest.raises(ValueError, match="key frame"):
        recorder._encode_nalu_window_sync("tool", window)
