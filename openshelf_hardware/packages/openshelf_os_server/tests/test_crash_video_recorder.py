from __future__ import annotations

import asyncio
import io
import threading
import time
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import av
import cv2
import numpy as np
import pytest

from openshelf_os_server.core.camera import (
    ToolCameraStreamManager,
    _au_contains_hevc_keyframe,
    camera_registry,
)
from openshelf_os_server.core.crash_video_recorder import (
    MIB,
    CrashVideoRecorder,
    CrashVideoRecorderConfig,
    RecordedCameraFrame,
)


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


def test_recorder_prunes_old_frames_and_enforces_camera_memory_limit() -> None:
    recorder = CrashVideoRecorder(recorder_config(max_camera_buffer_bytes=7))

    recorder._append_frame("tool", RecordedCameraFrame(80.0, b"old"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(99.0, b"1234"), now=100.0)
    recorder._append_frame("tool", RecordedCameraFrame(100.0, b"5678"), now=100.0)

    frames = recorder._buffers["tool"]
    assert [(frame.captured_at, frame.data) for frame in frames] == [(100.0, b"5678")]
    assert recorder._buffer_bytes["tool"] == 4


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


def _make_hevc_window(frame_count: int = 10, width: int = 160, height: int = 120):
    """Real Annex-B HEVC access units at 25 fps, every frame an IDR.

    g=1 forces a keyframe on every frame so the stream carries IDR
    NALs early, like the h264 helper's stream. Returns the (pts, unit)
    window, one demuxed access unit each, and the stream's Annex-B
    VPS/SPS/PPS extradata.
    """
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


