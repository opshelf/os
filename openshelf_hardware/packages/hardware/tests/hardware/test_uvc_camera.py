from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from hardware.exceptions import CameraConnectionError
from hardware.uvc_camera import UvcCamera


def test_uvc_camera_captures_and_normalizes_gray_frame() -> None:
    capture = MagicMock()
    capture.isOpened.return_value = True
    capture.read.return_value = (True, np.zeros((10, 20), dtype=np.uint8))

    with patch("hardware.uvc_camera.cv2.VideoCapture", return_value=capture):
        camera = UvcCamera(device=0, width=20, height=10, fps=30)
        camera.start()
        frame = camera.capture_raw()

    assert frame.shape == (10, 20, 3)


def test_uvc_camera_raises_when_device_cannot_open() -> None:
    capture = MagicMock()
    capture.isOpened.return_value = False

    with patch("hardware.uvc_camera.cv2.VideoCapture", return_value=capture):
        camera = UvcCamera(device=0, width=20, height=10, fps=30)
        with pytest.raises(CameraConnectionError):
            camera.start()
