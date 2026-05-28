import pytest
import numpy as np
import cv2
from hardware.exceptions import CameraConnectionError, CameraNotPresentError
from hardware.iocam import AFSCam, _resolve_innomaker_device
from unittest.mock import Mock, patch




@pytest.mark.asyncio
async def test_afscam_start_stop(sample_usb_params):
    with patch("cv2.VideoCapture") as mock_usb:
        mock_usb_instance = Mock()
        mock_usb_instance.isOpened.return_value = True
        mock_usb_instance.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        mock_usb.return_value = mock_usb_instance

        cam = AFSCam(sample_usb_params)

        await cam.start()
        mock_usb_instance.set.assert_any_call(cv2.CAP_PROP_AUTO_EXPOSURE, 3)

        await cam.stop()
        mock_usb_instance.release.assert_called_once()


@pytest.mark.asyncio
async def test_afscam_capture_raw(sample_usb_params):
    with patch("cv2.VideoCapture") as mock_usb:
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        mock_usb_instance = Mock()
        mock_usb_instance.isOpened.return_value = True
        mock_usb_instance.read.return_value = (True, frame)
        mock_usb.return_value = mock_usb_instance

        cam = AFSCam(sample_usb_params)
        await cam.start()

        out = cam.capture_raw()
        assert out is not None
        assert out.shape == frame.shape

        await cam.stop()


@pytest.mark.asyncio
async def test_afscam_start_fails_when_usb_unopened(sample_usb_params):
    with patch("cv2.VideoCapture") as mock_usb:
        mock_usb_instance = Mock()
        mock_usb_instance.isOpened.return_value = False
        mock_usb.return_value = mock_usb_instance

        cam = AFSCam(sample_usb_params)

        with pytest.raises(CameraConnectionError):
            await cam.start()


def test_resolve_innomaker_device_returns_index():
    output = """Innomaker-U20CAM-1080p-S1\n\t/dev/video4\n"""

    assert _resolve_innomaker_device(output) == 4


def test_resolve_innomaker_device_raises_when_missing():
    with pytest.raises(CameraNotPresentError):
        _resolve_innomaker_device("USB Camera\n\t/dev/video0\n")


def test_resolve_innomaker_device_raises_when_path_missing():
    output = """Innomaker-U20CAM-1080p-S1\n\t/dev/media0\n"""

    with pytest.raises(CameraConnectionError):
        _resolve_innomaker_device(output)
