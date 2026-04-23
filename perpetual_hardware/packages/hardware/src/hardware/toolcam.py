import logging
import cv2
import time
from pathlib import Path
import numpy.typing as npt

from hardware.exceptions import (
    CameraError, CameraConnectionError, ImageError
)

logger = logging.getLogger(__name__)

_FRAME_DEBUG_PATH = Path("/tmp") / "frame.png"


class ToolCamera:
    url: str

    def __init__(self, params) -> None:
        self.url = params.url

    async def capture(self) -> npt.NDArray:
        """
        Capture a frame from the camera.

        Returns:
            numpy.ndarray: The captured frame

        Raises:
            CameraConnectionError: If camera cannot be opened or initialized
            ImageError: If frame capture fails or returns invalid data
        """
        try:
            cap = cv2.VideoCapture(self.url)
            if not cap.isOpened():

                logger.warning("Failed to open camera at URL: %s", self.url)

                raise CameraConnectionError(
                    f"Failed to open camera at URL: {self.url}"
                )

            try:
                t0 = time.time()
                while time.time() - t0 < 4.25:
                    if not cap.grab():

                        logger.warning("Failed to grab frame during buffer clear for %s", self.url)

                        raise ImageError("Failed to grab frame during buffer clear")
                    time.sleep(0.001)

                for _ in range(5):
                    if not cap.grab():
                        raise ImageError("Failed to grab frame during refresh")

                ok, frame = cap.read()
                if not ok or frame is None:
                    raise ImageError(
                        "Failed to read frame or received empty frame"
                    )
                cv2.imwrite(str(_FRAME_DEBUG_PATH), frame)
                return frame

            finally:
                cap.release()

        except cv2.error as e:
            raise CameraError(f"OpenCV error: {str(e)}") from e
        except Exception as e:
            raise CameraError(f"Unexpected camera error: {str(e)}") from e
