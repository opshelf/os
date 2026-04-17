import pytest
import numpy as np
import cv2
from hardware.exceptions import CameraConnectionError, CameraNotPresentError, TOFError
from hardware.iocam import AFSCam, _resolve_innomaker_device
from unittest.mock import Mock, patch

@pytest.mark.asyncio
async def test_afscam_initialization(sample_tof_params, sample_usb_params):
    cam = AFSCam(sample_usb_params, sample_tof_params)
    
    # Check initialization of parameters
    assert cam.preferred_tof_port == sample_tof_params.csi_port
    assert cam.max_distance_mm == sample_tof_params.max_distance_mm
    assert cam.confidence_min == sample_tof_params.confidence_min
    assert cam.square_mm == sample_usb_params.square_mm
    assert cam.fx_px == sample_usb_params.fx_px
    assert cam.fy_px == sample_usb_params.fy_px

@pytest.mark.asyncio
async def test_afscam_start_stop(sample_tof_params, sample_usb_params):
    with patch('ArducamDepthCamera.ArducamCamera') as mock_tof, \
         patch('cv2.VideoCapture') as mock_usb:
        
        # Setup mock TOF camera
        mock_tof_instance = Mock()
        mock_tof_instance.start.return_value = 0
        mock_tof.return_value = mock_tof_instance
        
        # Setup mock USB camera
        mock_usb_instance = Mock()
        mock_usb_instance.isOpened.return_value = True
        mock_usb_instance.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8))
        mock_usb.return_value = mock_usb_instance
        
        # Create camera instance
        cam = AFSCam(sample_usb_params, sample_tof_params)
        
        # Test start
        await cam.start()
        mock_tof_instance.start.assert_called_once()
        mock_usb_instance.set.assert_any_call(cv2.CAP_PROP_EXPOSURE, 
                                            float(sample_usb_params.usb_exposure))
        
        # Test stop
        await cam.stop()
        mock_tof_instance.close.assert_called_once()

@pytest.mark.asyncio
async def test_afscam_capture_raw(sample_tof_params, sample_usb_params):
    with patch('ArducamDepthCamera.ArducamCamera') as mock_tof, \
         patch('cv2.VideoCapture') as mock_usb:
        mock_tof_instance = Mock()
        mock_tof_instance.start.return_value = 0
        mock_tof.return_value = mock_tof_instance

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        mock_usb_instance = Mock()
        mock_usb_instance.isOpened.return_value = True
        mock_usb_instance.read.return_value = (True, frame)
        mock_usb.return_value = mock_usb_instance

        cam = AFSCam(sample_usb_params, sample_tof_params)
        await cam.start()

        out = cam.capture_raw()
        assert out is not None
        assert out.shape == frame.shape

        await cam.stop()

@pytest.mark.asyncio
async def test_afscam_error_handling(sample_tof_params, sample_usb_params):
    with patch('ArducamDepthCamera.ArducamCamera') as mock_tof:
        # Setup mock TOF camera to fail
        mock_tof_instance = Mock()
        mock_tof_instance.start.return_value = 1  # Indicate failure
        mock_tof.return_value = mock_tof_instance
        
        # Create camera instance
        cam = AFSCam(sample_usb_params, sample_tof_params)
        
        # Test start should raise exception
        with pytest.raises(TOFError):
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
