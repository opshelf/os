import pytest
import numpy as np
from unittest.mock import Mock, patch
from hardware.exceptions import CameraError
from hardware.toolcam import ToolCamera

@pytest.mark.asyncio
async def test_toolcam_capture():
    params = Mock()
    params.url = 0
    camera = ToolCamera(params)

    with patch('cv2.VideoCapture') as mock_cap:
        mock_instance = Mock()
        mock_instance.isOpened.return_value = True
        mock_instance.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        mock_cap.return_value = mock_instance

        frame = await camera.capture()
        mock_cap.assert_called_once_with(0)
        assert frame is not None
        assert frame.shape == (480, 640, 3)

@pytest.mark.asyncio
async def test_toolcam_capture_failure():
    params = Mock()
    params.url = 0
    camera = ToolCamera(params)

    with patch('cv2.VideoCapture') as mock_cap:
        mock_instance = Mock()
        mock_instance.isOpened.return_value = True
        mock_instance.read.return_value = (False, None)
        mock_cap.return_value = mock_instance

        with pytest.raises(CameraError):
            await camera.capture()
