from __future__ import annotations

import asyncio
import io
import os
import re
import statistics
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Awaitable, Generator
from typing import Any, cast

import av
import cv2
import numpy as np

from openshelf_os.robot.models import (
    CrashReportArtifacts,
    CrashReportAttachment,
)
from openshelf_os_server.core.camera import (
    CameraManager,
    ToolCameraStreamManager,
    _all_consumer_counts,
    _au_contains_hevc_keyframe,
    _au_contains_idr,
    camera_registry,
    start_camera_stream_by_id,
)
from openshelf_os_server.core.logging_config import get_logger

logger = get_logger(__name__)

MIB = 1024 * 1024
DEFAULT_LOOKBACK_SECONDS = 15
DEFAULT_SAMPLE_FPS = 3
DEFAULT_MAX_WIDTH = 640
DEFAULT_MAX_CAMERA_BUFFER_BYTES = 16 * MIB
DEFAULT_MAX_ATTACHMENT_BYTES = 4 * MIB
DEFAULT_MAX_TOTAL_ATTACHMENT_BYTES = 14 * MIB
MAX_PENDING_CAPTURES = 2
CAMERA_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")

# Raw elementary-stream demuxer format per codec name, spelled as PyAV
# spells it (this av build registers the raw HEVC demuxer as "hevc").
# A codec name missing here is an unsupported window.
NALE_DEMUX_FORMAT_BY_CODEC = {"h264": "h264", "hevc": "hevc", "h265": "hevc"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _nalu_window_fps(window: NaluWindow) -> float:
    pts_list = [pts for pts, _data in window.frames]
    deltas = [
        later - earlier
        for earlier, later in zip(pts_list, pts_list[1:], strict=False)
        if 0 < (later - earlier) < 2.0
    ]

    if not deltas:
        return 25.0

    fps = 1.0 / statistics.median(deltas)
    return min(30.0, max(1.0, round(fps)))


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        logger.warning(
            "crash_recorder_invalid_integer", setting=name, value=value, fallback=default
        )
        return default
    return min(maximum, max(minimum, parsed))


@dataclass(frozen=True)
class CrashVideoRecorderConfig:
    enabled: bool = True
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS
    sample_fps: int = DEFAULT_SAMPLE_FPS
    max_width: int = DEFAULT_MAX_WIDTH
    max_camera_buffer_bytes: int = DEFAULT_MAX_CAMERA_BUFFER_BYTES
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    max_total_attachment_bytes: int = DEFAULT_MAX_TOTAL_ATTACHMENT_BYTES

    @classmethod
    def from_env(cls) -> "CrashVideoRecorderConfig":
        return cls(
            enabled=_env_bool("CRASH_RECORDER_ENABLED", True),
            lookback_seconds=_env_int(
                "CRASH_RECORDER_LOOKBACK_SECONDS",
                DEFAULT_LOOKBACK_SECONDS,
                minimum=3,
                maximum=60,
            ),
            sample_fps=_env_int(
                "CRASH_RECORDER_FPS",
                DEFAULT_SAMPLE_FPS,
                minimum=1,
                maximum=3,
            ),
            max_width=_env_int(
                "CRASH_RECORDER_MAX_WIDTH",
                DEFAULT_MAX_WIDTH,
                minimum=160,
                maximum=DEFAULT_MAX_WIDTH,
            ),
            max_camera_buffer_bytes=_env_int(
                "CRASH_RECORDER_MAX_CAMERA_BUFFER_MIB",
                DEFAULT_MAX_CAMERA_BUFFER_BYTES // MIB,
                minimum=2,
                maximum=128,
            )
            * MIB,
            max_attachment_bytes=_env_int(
                "CRASH_RECORDER_MAX_ATTACHMENT_MIB",
                DEFAULT_MAX_ATTACHMENT_BYTES // MIB,
                minimum=1,
                maximum=16,
            )
            * MIB,
            max_total_attachment_bytes=_env_int(
                "CRASH_RECORDER_MAX_TOTAL_ATTACHMENT_MIB",
                DEFAULT_MAX_TOTAL_ATTACHMENT_BYTES // MIB,
                minimum=1,
                maximum=18,
            )
            * MIB,
        )


@dataclass(frozen=True)
class RecordedCameraFrame:
    captured_at: float
    data: bytes


@dataclass(frozen=True)
class NaluWindow:
    """A frozen slice of a camera's live elementary access-unit ring.

    frames are (pts_seconds, access unit) tuples in stream order.
    au_is_avcc records the AUs' wire form: False (the default) means
    Annex-B start-code framing, True means AVCC (u32 length-prefixed
    NALs). codec_name is the demuxer's codec for the ring's source
    ("h264" or "hevc"); it selects the elementary-stream demuxer
    and the keyframe scan.
    """

    frames: tuple[tuple[float, bytes], ...]
    au_is_avcc: bool = False
    codec_name: str = "h264"

    def __len__(self) -> int:
        return len(self.frames)

    @property
    def duration_seconds(self) -> float:
        if len(self.frames) < 2:
            return 0.0

        return max(0.0, self.frames[-1][0] - self.frames[0][0])


class PreparedCrashCapture(Awaitable[CrashReportArtifacts]):
    """A frozen recorder snapshot whose encoding starts only when awaited."""

    def __init__(
        self,
        recorder: "CrashVideoRecorder",
        reason: str,
        crash_at_utc: datetime,
        frozen: dict[str, list[RecordedCameraFrame] | NaluWindow],
    ) -> None:
        self._recorder = recorder
        self._reason = reason
        self._crash_at_utc = crash_at_utc
        self._frozen = frozen
        self._task: asyncio.Task[CrashReportArtifacts] | None = None
        self._released = False
        self._cancelled = False
        self._consumer_cancelled = False

    def __await__(self) -> Generator[Any, None, CrashReportArtifacts]:
        if self._cancelled is True:
            raise asyncio.CancelledError
        if self._task is None:
            self._task = asyncio.create_task(self._encode(), name="crash-video-encode")
            self._task.add_done_callback(self._consume_background_task_exception)
        try:
            return (yield from asyncio.shield(self._task).__await__())
        except asyncio.CancelledError:
            self._consumer_cancelled = True
            raise

    def cancel(self) -> None:
        if self._task is None:
            self._cancelled = True
            self._release_pending()

    def _consume_background_task_exception(
        self, task: asyncio.Task[CrashReportArtifacts]
    ) -> None:
        if self._consumer_cancelled is False:
            return
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "crash_video_encoding_failed_after_consumer_cancelled",
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _release_pending(self) -> None:
        if self._released is False:
            self._released = True
            self._recorder._pending_captures -= 1
            self._frozen.clear()

    async def _encode(self) -> CrashReportArtifacts:
        try:
            return await self._recorder._encode_frozen_capture(
                self._reason,
                self._crash_at_utc,
                self._frozen,
            )
        finally:
            self._release_pending()


class CrashVideoRecorder:
    def __init__(self, config: CrashVideoRecorderConfig | None = None) -> None:
        self.config = config or CrashVideoRecorderConfig.from_env()
        self._buffers: dict[str, deque[RecordedCameraFrame]] = {}
        self._buffer_bytes: dict[str, int] = {}
        self._last_frame_at: dict[str, float] = {}
        self._owned_managers: dict[str, CameraManager] = {}
        self._sample_task: asyncio.Task[None] | None = None
        self._running = False
        self._capture_lock = asyncio.Lock()
        self._pending_captures = 0

    async def start(self) -> None:
        if self.config.enabled is False or self._running is True:
            return

        self._running = True
        await self._reconcile_cameras()
        self._sample_task = asyncio.create_task(
            self._sample_loop(),
            name="crash-video-recorder",
        )
        logger.info(
            "crash_video_recorder_started",
            lookback_seconds=self.config.lookback_seconds,
            sample_fps=self.config.sample_fps,
            camera_count=len(self._owned_managers),
        )

    async def stop(self) -> None:
        self._running = False
        if self._sample_task is not None:
            self._sample_task.cancel()
            try:
                await self._sample_task
            except asyncio.CancelledError:
                pass
            self._sample_task = None

        for manager in self._owned_managers.values():
            await self._release_manager_lease(manager)

        self._owned_managers.clear()
        self._buffers.clear()
        self._buffer_bytes.clear()
        self._last_frame_at.clear()
        logger.info("crash_video_recorder_stopped")

    async def _reconcile_cameras(self) -> None:
        for camera_id, manager in list(self._owned_managers.items()):
            if camera_registry.get(camera_id) is not manager:
                await self._release_manager_lease(manager)
                self._owned_managers.pop(camera_id, None)
                self._buffers.pop(camera_id, None)
                self._buffer_bytes.pop(camera_id, None)
                self._last_frame_at.pop(camera_id, None)

        for camera_id, manager in list(camera_registry.items()):
            if self._owned_managers.get(camera_id) is manager:
                continue
            try:
                await start_camera_stream_by_id(camera_id, background=True)
            except Exception as exc:
                logger.warning(
                    "crash_recorder_camera_start_failed",
                    camera_id=camera_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                continue
            self._owned_managers[camera_id] = manager
            self._buffers.setdefault(camera_id, deque())
            self._buffer_bytes.setdefault(camera_id, 0)

    async def _release_manager_lease(self, manager: CameraManager) -> None:
        manager.decrement_background_consumers()
        mjpeg_consumers, nalu_consumers, background_consumers = _all_consumer_counts(
            manager
        )
        if (
            mjpeg_consumers == 0
            and nalu_consumers == 0
            and background_consumers == 0
            and manager.is_running is True
        ):
            try:
                await manager.stop()
            except Exception as exc:
                logger.warning(
                    "crash_recorder_camera_stop_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    async def _sample_loop(self) -> None:
        sample_interval = 1.0 / self.config.sample_fps
        while self._running is True:
            loop_started_at = time.monotonic()
            await self._reconcile_cameras()
            await self._sample_once(loop_started_at)
            elapsed = time.monotonic() - loop_started_at
            await asyncio.sleep(max(0.0, sample_interval - elapsed))

    async def _sample_once(self, sampled_at: float | None = None) -> None:
        now = time.monotonic() if sampled_at is None else sampled_at
        for camera_id, manager in list(self._owned_managers.items()):
            # RTSP cameras are sampled on demand from their NALU ring at
            # freeze time; only JPEG-ring cameras (UVC) sample here.
            if isinstance(manager, ToolCameraStreamManager) is True:
                continue

            frame = await manager.get_latest_frame()
            frame_at = manager.last_frame_at
            if frame is None or frame_at is None:
                continue
            if self._last_frame_at.get(camera_id) == frame_at:
                continue
            self._last_frame_at[camera_id] = frame_at
            self._append_frame(
                camera_id, RecordedCameraFrame(captured_at=frame_at, data=frame), now
            )

    def _append_frame(self, camera_id: str, frame: RecordedCameraFrame, now: float) -> None:
        frames = self._buffers.setdefault(camera_id, deque())
        frames.append(frame)
        self._buffer_bytes[camera_id] = self._buffer_bytes.get(camera_id, 0) + len(frame.data)
        cutoff = now - self.config.lookback_seconds

        while frames and (
            frames[0].captured_at < cutoff
            or self._buffer_bytes[camera_id] > self.config.max_camera_buffer_bytes
        ):
            removed = frames.popleft()
            self._buffer_bytes[camera_id] -= len(removed.data)

    def _freeze_buffers(
        self, crash_at: float
    ) -> dict[str, list[RecordedCameraFrame] | NaluWindow]:
        frozen: dict[str, list[RecordedCameraFrame] | NaluWindow] = {}
        cutoff = crash_at - self.config.lookback_seconds
        for camera_id, frames in self._buffers.items():
            if camera_registry.get(camera_id) is not self._owned_managers.get(camera_id):
                continue
            selected = [frame for frame in frames if cutoff <= frame.captured_at <= crash_at]
            if selected:
                frozen[camera_id] = selected

        for camera_id, manager in self._owned_managers.items():
            if not isinstance(manager, ToolCameraStreamManager):
                continue
            if camera_registry.get(camera_id) is not manager:
                continue
            if frozen.get(camera_id) is not None:
                continue
            window = manager.get_nalu_window(self.config.lookback_seconds)
            if len(window) >= 2:
                frozen[camera_id] = NaluWindow(
                    frames=tuple(window),
                    au_is_avcc=manager._nalu_au_is_annexb() is False,
                    codec_name=manager.nalu_window_codec() or "h264",
                )

        return frozen

    def begin_capture(self, reason: str) -> PreparedCrashCapture | CrashReportArtifacts:
        if self.config.enabled is False:
            return CrashReportArtifacts(context={"status": "disabled"})
        if self._running is False:
            return CrashReportArtifacts(context={"status": "not_running"})

        if self._pending_captures >= MAX_PENDING_CAPTURES:
            return CrashReportArtifacts(
                context={
                    "status": "queue_saturated",
                    "reason": reason,
                    "max_pending_captures": MAX_PENDING_CAPTURES,
                    "attachment_count": 0,
                }
            )

        self._pending_captures += 1
        try:
            # Freeze references before waiting for a prior crash's sequential encode.
            # The recorder loop and capture run on the same event loop, so no lock is
            # needed between taking the timestamp and copying these frame references.
            crash_at_monotonic = time.monotonic()
            crash_at_utc = datetime.now(timezone.utc)
            frozen = self._freeze_buffers(crash_at_monotonic)
            return PreparedCrashCapture(self, reason, crash_at_utc, frozen)
        except Exception:
            self._pending_captures -= 1
            raise

    async def capture(self, reason: str) -> CrashReportArtifacts:
        prepared = self.begin_capture(reason)
        if isinstance(prepared, CrashReportArtifacts):
            return prepared
        return await prepared

    async def _encode_frozen_capture(
        self,
        reason: str,
        crash_at_utc: datetime,
        frozen: dict[str, list[RecordedCameraFrame] | NaluWindow],
    ) -> CrashReportArtifacts:
        try:
            context: dict[str, object] = {
                "status": "captured" if frozen else "no_frames",
                "crash_at": crash_at_utc.isoformat(),
                "lookback_seconds": self.config.lookback_seconds,
                "sample_fps": self.config.sample_fps,
                "camera_count": len(frozen),
                "cameras": {},
            }
            camera_context = context["cameras"]
            assert isinstance(camera_context, dict)
            attachments: list[CrashReportAttachment] = []
            total_attachment_bytes = 0

            async with self._capture_lock:
                for camera_id, frames in frozen.items():
                    if isinstance(frames, NaluWindow):
                        frame_count = len(frames.frames)
                        duration_seconds = frames.duration_seconds
                        details: dict[str, object] = {
                            "frame_count": frame_count,
                            "duration_seconds": round(duration_seconds, 3),
                            "buffer_bytes": sum(len(data) for _pts, data in frames.frames),
                        }
                    else:
                        frame_count = len(frames)
                        duration_seconds = 0.0
                        if frame_count > 1:
                            duration_seconds = frames[-1].captured_at - frames[0].captured_at
                        details = {
                            "frame_count": frame_count,
                            "duration_seconds": round(duration_seconds, 3),
                            "buffer_bytes": sum(len(frame.data) for frame in frames),
                        }
                    camera_context[camera_id] = details

                    try:
                        remaining_attachment_bytes = max(
                            0,
                            self.config.max_total_attachment_bytes - total_attachment_bytes,
                        )
                        if isinstance(frames, NaluWindow):
                            video_bytes = await self._encode_nalu_window(
                                camera_id,
                                frames,
                                max_attachment_bytes=min(
                                    self.config.max_attachment_bytes,
                                    remaining_attachment_bytes,
                                ),
                            )
                        else:
                            video_bytes = await self._encode_camera(
                                camera_id,
                                frames,
                                max_attachment_bytes=min(
                                    self.config.max_attachment_bytes,
                                    remaining_attachment_bytes,
                                ),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        details.update(
                            {
                                "attached": False,
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                            }
                        )
                        logger.warning(
                            "crash_video_encoding_failed",
                            camera_id=camera_id,
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                        continue

                    if len(video_bytes) > self.config.max_attachment_bytes:
                        details.update(
                            {
                                "attached": False,
                                "error": "attachment_too_large",
                                "video_bytes": len(video_bytes),
                            }
                        )
                        continue
                    if (
                        total_attachment_bytes + len(video_bytes)
                        > self.config.max_total_attachment_bytes
                    ):
                        details.update(
                            {
                                "attached": False,
                                "error": "total_attachment_budget_exceeded",
                                "video_bytes": len(video_bytes),
                            }
                        )
                        continue

                    safe_camera_id = (
                        CAMERA_ID_PATTERN.sub("-", camera_id).strip("-") or "camera"
                    )
                    timestamp = crash_at_utc.strftime("%Y%m%dT%H%M%S.%fZ")
                    filename = f"{safe_camera_id}-motion-crash-{timestamp}.webm"
                    attachments.append(
                        CrashReportAttachment(
                            filename=filename,
                            data=video_bytes,
                            content_type="video/webm",
                        )
                    )
                    total_attachment_bytes += len(video_bytes)
                    details.update({"attached": True, "video_bytes": len(video_bytes)})

            context["attachment_count"] = len(attachments)
            context["attachment_bytes"] = total_attachment_bytes
            context["reason"] = reason
            return CrashReportArtifacts(attachments=attachments, context=context)
        finally:
            pass

    async def _encode_camera(
        self,
        camera_id: str,
        frames: list[RecordedCameraFrame],
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        return await asyncio.to_thread(
            self._encode_camera_sync,
            camera_id,
            frames,
            max_attachment_bytes=max_attachment_bytes,
        )

    async def _encode_nalu_window(
        self,
        camera_id: str,
        window: NaluWindow,
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        return await asyncio.to_thread(
            self._encode_nalu_window_sync,
            camera_id,
            window,
            max_attachment_bytes=max_attachment_bytes,
        )

    def _encode_nalu_window_sync(
        self,
        camera_id: str,
        window: NaluWindow,
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        if len(window.frames) < 2:
            raise ValueError("at least two access units are required")

        demux_format = NALE_DEMUX_FORMAT_BY_CODEC.get(window.codec_name)
        if demux_format is None:
            raise ValueError(f"unsupported NALU codec: {window.codec_name}")
        is_hevc = demux_format == "hevc"

        # A byte-capped window can start mid-GOP, past every key
        # frame. No key frame means no packet can decode, so fail with
        # a reason the artifact can record instead of a codec parse
        # error from the first P-slice.
        if is_hevc:
            has_key_frame = any(
                _au_contains_hevc_keyframe(data) for _pts, data in window.frames
            )
        else:
            has_key_frame = any(
                _au_contains_idr(data, window.au_is_avcc)
                for _pts, data in window.frames
            )
        if not has_key_frame:
            raise ValueError("NALU window contains no key frame")

        safe_camera_id = CAMERA_ID_PATTERN.sub("-", camera_id).strip("-") or "camera"
        descriptor, output_name = tempfile.mkstemp(prefix=f"crash-{safe_camera_id}-", suffix=".webm")
        os.close(descriptor)
        output_path = Path(output_name)
        writer: cv2.VideoWriter | None = None

        try:
            # The access units concatenate into a valid h264/hevc
            # elementary stream, so a single decode pass over the frozen
            # window works.
            elementary = b"".join(data for _pts, data in window.frames)
            container = cast(
                av.container.InputContainer,
                av.open(io.BytesIO(elementary), format=demux_format),
            )

            fps = _nalu_window_fps(window)
            output_size: tuple[int, int] | None = None
            decoded_frame_count = 0

            # Decode per packet so one structurally bad AU (RTSP packet
            # loss, mid-window reconnect) loses only its own frames
            # instead of aborting the whole window. The raw h264/hevc
            # demuxer leaves dts unset, so every packet gets a decode
            # attempt.
            stream = container.streams.video[0]
            for packet in container.demux(stream):
                try:
                    for decoded in stream.codec_context.decode(packet):
                        if decoded is None:
                            continue
                        frame = decoded.to_ndarray(format="bgr24")

                        if output_size is None:
                            source_height, source_width = frame.shape[:2]
                            scale = min(1.0, self.config.max_width / source_width)
                            output_width = max(2, int(source_width * scale))
                            output_height = max(2, int(source_height * scale))
                            output_width -= output_width % 2
                            output_height -= output_height % 2
                            output_size = (output_width, output_height)
                            writer = cv2.VideoWriter(
                                str(output_path),
                                cast(Any, cv2).VideoWriter_fourcc(*"VP80"),
                                fps,
                                output_size,
                            )
                            if writer.isOpened() is False:
                                raise RuntimeError(
                                    "OpenCV VP8 video encoder is unavailable"
                                )

                        resized: np.ndarray
                        if (frame.shape[1], frame.shape[0]) != output_size:
                            resized = cv2.resize(
                                frame, output_size, interpolation=cv2.INTER_AREA
                            )
                        else:
                            resized = frame

                        assert writer is not None
                        writer.write(resized)
                        decoded_frame_count += 1
                        del decoded
                except av.error.InvalidDataError:
                    continue

            if writer is None:
                raise ValueError("no decodable frames in NALU window")
            if decoded_frame_count < 2:
                raise ValueError("at least two decodable frames are required")

            writer.release()
            writer = None

            effective_max_attachment_bytes = (
                self.config.max_attachment_bytes
                if max_attachment_bytes is None
                else max_attachment_bytes
            )
            if output_path.stat().st_size > effective_max_attachment_bytes:
                raise ValueError("encoded attachment exceeds size budget")
            return output_path.read_bytes()
        finally:
            if writer is not None:
                writer.release()
            output_path.unlink(missing_ok=True)

    def _encode_camera_sync(
        self,
        camera_id: str,
        frames: list[RecordedCameraFrame],
        *,
        max_attachment_bytes: int | None = None,
    ) -> bytes:
        if len(frames) < 2:
            raise ValueError("at least two frames are required")

        safe_camera_id = CAMERA_ID_PATTERN.sub("-", camera_id).strip("-") or "camera"
        descriptor, output_name = tempfile.mkstemp(prefix=f"crash-{safe_camera_id}-", suffix=".webm")
        os.close(descriptor)
        output_path = Path(output_name)
        writer: cv2.VideoWriter | None = None
        try:
            output_size: tuple[int, int] | None = None
            decoded_frame_count = 0
            for recorded_frame in frames:
                decoded = cv2.imdecode(
                    np.frombuffer(recorded_frame.data, dtype=np.uint8),
                    cv2.IMREAD_COLOR,
                )
                if decoded is None:
                    continue

                if output_size is None:
                    source_height, source_width = decoded.shape[:2]
                    scale = min(1.0, self.config.max_width / source_width)
                    output_width = max(2, int(source_width * scale))
                    output_height = max(2, int(source_height * scale))
                    output_width -= output_width % 2
                    output_height -= output_height % 2
                    output_size = (output_width, output_height)
                    writer = cv2.VideoWriter(
                        str(output_path),
                        cast(Any, cv2).VideoWriter_fourcc(*"VP80"),
                        self.config.sample_fps,
                        output_size,
                    )
                    if writer.isOpened() is False:
                        raise RuntimeError("OpenCV VP8 video encoder is unavailable")

                if (decoded.shape[1], decoded.shape[0]) != output_size:
                    decoded = cv2.resize(decoded, output_size, interpolation=cv2.INTER_AREA)
                assert writer is not None
                writer.write(decoded)
                decoded_frame_count += 1
                del decoded

            if decoded_frame_count < 2:
                raise ValueError("at least two decodable frames are required")

            assert writer is not None
            writer.release()
            writer = None
            effective_max_attachment_bytes = (
                self.config.max_attachment_bytes
                if max_attachment_bytes is None
                else max_attachment_bytes
            )
            if output_path.stat().st_size > effective_max_attachment_bytes:
                raise ValueError("encoded attachment exceeds size budget")
            return output_path.read_bytes()
        finally:
            if writer is not None:
                writer.release()
            output_path.unlink(missing_ok=True)


_recorder: CrashVideoRecorder | None = None


async def start_crash_video_recorder() -> CrashVideoRecorder | None:
    global _recorder
    config = CrashVideoRecorderConfig.from_env()
    if config.enabled is False:
        logger.info("crash_video_recorder_disabled")
        return None
    if _recorder is None:
        _recorder = CrashVideoRecorder(config)
    await _recorder.start()
    return _recorder


async def stop_crash_video_recorder() -> None:
    global _recorder
    recorder = _recorder
    _recorder = None
    if recorder is not None:
        await recorder.stop()


async def capture_motion_crash_artifacts(reason: str) -> CrashReportArtifacts:
    recorder = _recorder
    if recorder is None:
        return CrashReportArtifacts(context={"status": "not_running"})
    return await recorder.capture(reason)


def begin_motion_crash_artifacts(
    reason: str,
) -> PreparedCrashCapture | asyncio.Future[CrashReportArtifacts]:
    recorder = _recorder
    if recorder is not None:
        prepared = recorder.begin_capture(reason)
        if isinstance(prepared, PreparedCrashCapture):
            return prepared

        completed = asyncio.get_running_loop().create_future()
        completed.set_result(prepared)
        return completed

    completed = asyncio.get_running_loop().create_future()
    completed.set_result(CrashReportArtifacts(context={"status": "not_running"}))
    return completed
