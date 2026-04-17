import logging
import re
import subprocess
from typing import Optional

import cv2
import numpy as np

from hardware.exceptions import (
    CameraError,
    CameraConnectionError,
    CameraConfigError,
    CameraNotPresentError,
)

logger = logging.getLogger(__name__)


def _resolve_innomaker_device(output: str) -> int:
    lines = output.splitlines()

    for i, line in enumerate(lines):
        if "Innomaker-U20CAM-1080p-S1" not in line:
            continue

        if i + 1 >= len(lines):
            break

        match = re.search(r"/dev/video(\d+)", lines[i + 1])
        if match is not None:
            return int(match.group(1))

        raise CameraConnectionError("Failed to parse Innomaker camera device path.")

    raise CameraNotPresentError("Innomaker U20CAM not found in v4l2-ctl output.")


def _find_innomaker_device() -> int:
    """Return the /dev/video index for the Innomaker camera."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as e:
        raise CameraConnectionError("v4l2-ctl is not installed.") from e
    except subprocess.CalledProcessError as e:
        raise CameraConnectionError(f"Failed to run v4l2-ctl: {e}") from e

    return _resolve_innomaker_device(result.stdout)


class AFSCam:
    """
    IO box USB color camera (Innomaker U20CAM) for monitoring / debugging.

    - await start()
    - capture_raw() for unprocessed BGR frames
    - await stop()
    """

    def __init__(self, usb_params) -> None:
        self.square_mm = usb_params.square_mm
        self.fx_px = usb_params.fx_px
        self.fy_px = usb_params.fy_px
        self.px_per_mm = usb_params.px_per_mm

        self._usb_index = usb_params.usb_index
        self._usb_fourcc = usb_params.usb_fourcc
        self._usb_exposure = usb_params.usb_exposure

        self._usb: Optional[cv2.VideoCapture] = None

    def _init_usb(self, *, use_auto_exposure: bool = True) -> None:
        """Initialize USB camera (auto-detect Innomaker U20CAM)."""
        try:
            usb_index = _find_innomaker_device()
            self._usb_index = usb_index
            logger.info("Found Innomaker U20CAM at /dev/video%s", usb_index)

            self._usb = cv2.VideoCapture(usb_index, cv2.CAP_V4L2)
            if not self._usb.isOpened():
                raise CameraConnectionError(f"Failed to open Innomaker at index {usb_index}")

            if self._usb_fourcc:
                fourcc = cv2.VideoWriter_fourcc(*self._usb_fourcc)
                if not self._usb.set(cv2.CAP_PROP_FOURCC, fourcc):
                    raise CameraConfigError(f"Failed to set FOURCC to {self._usb_fourcc}")

            if use_auto_exposure is True:
                self._usb.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
            elif self._usb_exposure is not None:
                if not self._usb.set(cv2.CAP_PROP_EXPOSURE, float(self._usb_exposure)):
                    raise CameraConfigError(f"Failed to set exposure {self._usb_exposure}")

            ok, frame0 = self._usb.read()
            if not ok or frame0 is None:
                raise CameraError("Failed to capture initial frame")

        except Exception as e:
            self._cleanup_usb()
            if not isinstance(e, (CameraConnectionError, CameraConfigError, CameraNotPresentError)):
                raise CameraError(f"USB camera initialization failed: {e}") from e
            raise

    async def start(self) -> None:
        """Open the USB camera with auto-exposure (typical monitoring setup)."""
        try:
            self._init_usb(use_auto_exposure=True)
        except Exception as e:
            if not isinstance(e, CameraError):
                raise CameraError(f"Camera initialization failed: {str(e)}") from e
            raise

    async def stop(self) -> None:
        try:
            self._cleanup_usb()
        except Exception as e:
            if not isinstance(e, CameraError):
                raise CameraError(f"Failed to stop cameras: {str(e)}") from e
            raise

    def capture_raw(self) -> Optional[np.ndarray]:
        """Capture a single raw frame from USB camera without any processing."""
        if self._usb is None:
            return None

        ok, frame = self._usb.read()
        if ok is True and frame is not None:
            return frame

        return None

    def _cleanup_usb(self) -> None:
        if self._usb is not None:
            try:
                self._usb.release()
            except Exception:
                pass
            self._usb = None
