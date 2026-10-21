from __future__ import annotations

import threading
from typing import Union

import cv2
import numpy as np

from hardware.exceptions import CameraConfigError, CameraConnectionError, ImageError


class UvcCamera:
    def __init__(
        self,
        *,
        device: Union[str, int],
        width: int,
        height: int,
        fps: int,
        flip_horizontal: bool = False,
        buffer_size: int = 1,
    ) -> None:
        if width <= 0 or height <= 0 or fps <= 0 or buffer_size <= 0:
            raise CameraConfigError("camera width, height, fps, and buffer_size must be positive")

        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.flip_horizontal = flip_horizontal
        self.buffer_size = buffer_size
        self._capture: cv2.VideoCapture | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._open_locked()

    def reacquire(self) -> None:
        """Re-open the device after a failed read, so a reconnected camera is usable again.

        A capture opened on a USB camera that is then unplugged never reads again: its device
        node is gone, so every later read fails even after the camera is plugged back in. The
        capture loop retries on its own and calls this before each read, so a reconnect heals
        the camera without an API restart. A handle that is already closed is left to start().
        """
        with self._lock:
            if self._capture is None or self._capture.isOpened() is False:
                return

            old_capture = self._capture

            try:
                self._capture = self._open_capture()
            except CameraConnectionError:
                self._capture = None
            finally:
                # The old handle may already sit on the gone device node, so it is released
                # whether or not the re-open succeeded; a failed one leaves the next retry to
                # open the device fresh and the read to report the failure as usual.
                old_capture.release()

    def stop(self) -> None:
        with self._lock:
            self._release_locked()

    def _open_capture(self) -> cv2.VideoCapture:
        """Open and configure the device handle. Raises when the device cannot be opened."""
        capture = cv2.VideoCapture(self.device)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        capture.set(cv2.CAP_PROP_BUFFERSIZE, self.buffer_size)

        if capture.isOpened() is False:
            capture.release()
            raise CameraConnectionError(f"failed to open UVC camera {self.device!r}")

        return capture

    def _open_locked(self) -> None:
        if self._capture is not None and self._capture.isOpened() is True:
            return

        self._capture = self._open_capture()

    def _release_locked(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def capture_raw(self) -> np.ndarray:
        with self._lock:
            if self._capture is None or self._capture.isOpened() is False:
                raise CameraConnectionError("UVC camera is not started")

            ok, frame = self._capture.read()

        if ok is False or frame is None:
            raise ImageError("failed to capture UVC camera frame")

        normalized = self._normalize_frame(frame)
        if self.flip_horizontal is True:
            normalized = cv2.flip(normalized, 1)

        return normalized

    def _normalize_frame(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 2:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

        if frame.ndim != 3:
            raise ImageError(f"unsupported frame shape: {frame.shape}")

        if frame.shape[2] == 1:
            return cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)

        if frame.shape[2] == 4:
            return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        if frame.shape[2] == 3:
            return frame

        raise ImageError(f"unsupported frame channel count: {frame.shape[2]}")
