import cv2
import numpy as np

from openshelf_os.config.models import IOBoxSizerDetectionConfig
from openshelf_os.iobox_sizer.vision import detect_bright_object


def test_detect_bright_object_inside_roi() -> None:
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    cv2.rectangle(frame, (40, 30), (79, 69), (255, 255, 255), -1)
    config = IOBoxSizerDetectionConfig(
        roi_x=20,
        roi_y=20,
        roi_width=80,
        roi_height=70,
        min_area_px=100,
    )

    result = detect_bright_object(frame, config)

    assert result.detected is True
    assert result.width_px >= 38
    assert result.height_px >= 38
    assert result.center_x == 60
    assert result.center_y == 50


def test_detect_bright_object_returns_false_for_empty_frame() -> None:
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    config = IOBoxSizerDetectionConfig(roi_x=0, roi_y=0, roi_width=120, roi_height=100)

    result = detect_bright_object(frame, config)

    assert result.detected is False


def test_detect_bright_object_clips_roi_to_frame() -> None:
    frame = np.zeros((60, 60, 3), dtype=np.uint8)
    cv2.rectangle(frame, (50, 50), (59, 59), (255, 255, 255), -1)
    config = IOBoxSizerDetectionConfig(
        roi_x=45,
        roi_y=45,
        roi_width=50,
        roi_height=50,
        min_area_px=20,
    )

    result = detect_bright_object(frame, config)

    assert result.detected is True
    assert result.center_x >= 54
    assert result.center_y >= 54
