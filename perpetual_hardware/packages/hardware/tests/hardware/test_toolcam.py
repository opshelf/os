import pytest
import cv2
import numpy as np
from unittest.mock import Mock, patch
from hardware.exceptions import CameraError
from hardware.toolcam import ToolCamera

def test_toolcam_initialization(sample_camera_params):
    camera = ToolCamera(sample_camera_params)
    assert camera.url == sample_camera_params['url']
    assert np.array_equal(camera.mtx, sample_camera_params['mtx'])
    assert np.array_equal(camera.dist, sample_camera_params['dist'])
    assert camera.board_W_mm == sample_camera_params['board_W_mm']
    assert camera.board_H_mm == sample_camera_params['board_H_mm']

@pytest.mark.asyncio
async def test_toolcam_capture():
    params = Mock()
    params.url = 0
    camera = ToolCamera(params)
    
    with patch('cv2.VideoCapture') as mock_cap:
        # Setup mock camera
        mock_instance = Mock()
        mock_instance.isOpened.return_value = True
        mock_instance.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        mock_cap.return_value = mock_instance

        frame = await camera.capture()
        assert frame is not None
        assert frame.shape == (480, 640, 3)

def test_toolcam_build_remap(sample_camera_params):
    camera = ToolCamera(sample_camera_params)
    test_size = (800, 600)
    camera._build_remap(test_size)
    
    assert camera.map_size == test_size
    assert camera.map1 is not None
    assert camera.map2 is not None
    assert isinstance(camera.newcameramtx, np.ndarray)
    assert isinstance(camera.roi, tuple)

@pytest.mark.asyncio
async def test_toolcam_capture_failure():
    params = Mock()
    params.url = 0
    camera = ToolCamera(params)
    
    with patch('cv2.VideoCapture') as mock_cap:
        # Setup mock camera to simulate failure
        mock_instance = Mock()
        mock_instance.isOpened.return_value = True
        mock_instance.read.return_value = (False, None)
        mock_cap.return_value = mock_instance

        with pytest.raises(CameraError):
            await camera.capture()
