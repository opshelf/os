import asyncio
import logging
import re
import subprocess
from typing import Any, Optional, TYPE_CHECKING

import cv2
import numpy as np

try:
    import ArducamDepthCamera as ac
except ImportError:
    ac = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from ArducamDepthCamera import ArducamCamera  # type: ignore[misc]
else:
    ArducamCamera = Any  # type: ignore

from hardware.exceptions import (
    CameraError,
    CameraConnectionError,
    CameraConfigError,
    CameraNotPresentError,
    TOFError,
    TOFConfigError,
    TOFDataError,
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
    IO box USB color camera with optional TOF depth sensor.

    - await start(enable_tof=...)
    - capture_raw() for unprocessed BGR frames
    - await stop()
    """

    def __init__(self, usb_params, tof_params=None) -> None:
        self._tof_params = tof_params

        if tof_params is not None:
            self.preferred_tof_port = getattr(tof_params, "csi_port", None)
            self.max_distance_mm = tof_params.max_distance_mm
            self.confidence_min = tof_params.confidence_min
        else:
            self.preferred_tof_port = None
            self.max_distance_mm = 620
            self.confidence_min = 0

        self.square_mm = usb_params.square_mm
        self.fx_px = usb_params.fx_px
        self.fy_px = usb_params.fy_px
        self.px_per_mm = usb_params.px_per_mm

        self._usb_index = usb_params.usb_index
        self._usb_fourcc = usb_params.usb_fourcc
        self._usb_exposure = usb_params.usb_exposure

        self._tof: Optional[ArducamCamera] = None
        self._usb: Optional[cv2.VideoCapture] = None

        self._depth: Optional[np.ndarray] = None
        self._conf: Optional[np.ndarray] = None
        self._depth_h: int = 0
        self._depth_w: int = 0

        self._loop_task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

    def _init_tof(self) -> None:
        """Initialize TOF camera."""
        if ac is None:
            raise TOFError(
                "ArducamDepthCamera is not installed. Install the hardware extras to "
                "enable TOF camera support."
            )
        try:
            self._tof = ac.ArducamCamera()
            port_used = self._open_tof(self._tof, self.preferred_tof_port)
            if port_used < 0:
                raise TOFError(f"Failed to open TOF on port {self.preferred_tof_port}")

            if self._tof.start(ac.FrameType.DEPTH) != 0:
                self._tof.close()
                logger.error("Failed to start TOF stream")
                raise TOFError("Failed to start TOF stream")

            try:
                self._tof.setControl(ac.Control.RANGE, self.max_distance_mm)
            except Exception as e:
                raise TOFConfigError(
                    f"Failed to set TOF range to {self.max_distance_mm}mm: {str(e)}"
                ) from e

            fr = self._tof.requestFrame(2000)
            if fr is None:
                raise TOFDataError("Failed to get initial TOF frame")

            if not isinstance(fr, ac.DepthData):
                self._tof.releaseFrame(fr)
                raise TOFDataError("Invalid TOF frame type")

            try:
                self._update_depth_buffers(fr.depth_data, fr.confidence_data)
            finally:
                self._tof.releaseFrame(fr)

        except Exception as e:
            self._cleanup_tof()
            if not isinstance(e, TOFError):
                raise TOFError(f"TOF initialization failed: {str(e)}") from e
            raise

    def _init_usb(self, use_auto_exposure: bool = False) -> None:
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

    async def start(self, enable_tof: bool = True) -> None:
        """
        Initialize and start cameras.

        Args:
            enable_tof: If True, initialize TOF camera (when tof_params were provided).
                       If False, only USB camera is initialized (monitoring mode with auto-exposure).
        """
        try:
            use_auto_exposure = enable_tof is False
            self._init_usb(use_auto_exposure=use_auto_exposure)

            if enable_tof is True and self._tof_params is not None:
                self._init_tof()
                self._stop_evt.clear()
                self._loop_task = asyncio.create_task(self._tof_loop())

        except Exception as e:
            self._cleanup()
            if not isinstance(e, (TOFError, CameraError)):
                raise CameraError(f"Camera initialization failed: {str(e)}") from e
            raise

    def _cleanup(self) -> None:
        self._cleanup_tof()
        self._cleanup_usb()

    async def stop(self) -> None:
        try:
            self._stop_evt.set()

            if self._loop_task:
                try:
                    await asyncio.wait_for(self._loop_task, timeout=1.5)
                except asyncio.TimeoutError:
                    self._loop_task.cancel()
                    await asyncio.sleep(0.1)
                except Exception as e:
                    raise CameraError(f"Error stopping background task: {str(e)}") from e
                finally:
                    self._loop_task = None

            try:
                self._cleanup_usb()
                self._cleanup_tof()
            except Exception as e:
                raise CameraError(f"Error during cleanup: {str(e)}") from e

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

    def _open_tof(self, cam: ArducamCamera, preferred_port: Optional[int]) -> int:
        ports_to_try = [preferred_port] if preferred_port is not None else [0, 1, 2]
        last_err = None
        for p in ports_to_try:
            ret = cam.open(ac.Connection.CSI, p)
            if ret == 0:
                return p
            last_err = ret

        logger.warning("Failed to open TOF on any CSI port. Last error: %s", last_err)

        return -1

    def _update_depth_buffers(self, depth: np.ndarray, conf: np.ndarray) -> None:
        self._depth = depth.copy()
        self._conf = conf.copy()
        self._depth_h, self._depth_w = self._depth.shape[:2]

    async def _tof_loop(self) -> None:
        assert self._tof is not None
        try:
            while not self._stop_evt.is_set():
                fr = self._tof.requestFrame(200)
                if fr is not None and isinstance(fr, ac.DepthData):
                    self._update_depth_buffers(fr.depth_data, fr.confidence_data)
                    self._tof.releaseFrame(fr)
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            pass

    def _cleanup_tof(self) -> None:
        if self._tof is not None:
            try:
                self._tof.stop()
            except Exception:
                pass
            try:
                self._tof.close()
            except Exception:
                pass
            self._tof = None

    def _cleanup_usb(self) -> None:
        if self._usb is not None:
            try:
                self._usb.release()
            except Exception:
                pass
            self._usb = None
