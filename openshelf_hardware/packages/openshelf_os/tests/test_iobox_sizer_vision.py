import cv2
import numpy as np

from openshelf_os.config.models import IOBoxSizerDetectionConfig
from openshelf_os.iobox_sizer.vision import detect_bright_object, detect_object


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


def test_detect_difference_object_uses_blank_frame(tmp_path) -> None:
    blank = np.full((100, 120, 3), 220, dtype=np.uint8)
    frame = blank.copy()
    cv2.rectangle(frame, (45, 35), (74, 64), (20, 20, 20), -1)

    blank_path = tmp_path / "blank.png"
    cv2.imwrite(str(blank_path), blank)
    config = IOBoxSizerDetectionConfig(
        mode="difference",
        roi_x=0,
        roi_y=0,
        roi_width=120,
        roi_height=100,
        diff_threshold=30,
        min_area_px=100,
        blank_frame_path=str(blank_path),
    )

    result = detect_object(frame, config)

    assert result.detected is True
    assert result.center_x == 60
    assert result.center_y == 50


def test_detect_difference_object_returns_false_without_blank() -> None:
    frame = np.zeros((100, 120, 3), dtype=np.uint8)
    cv2.rectangle(frame, (40, 30), (79, 69), (255, 255, 255), -1)
    config = IOBoxSizerDetectionConfig(
        mode="difference",
        roi_x=0,
        roi_y=0,
        roi_width=120,
        roi_height=100,
        min_area_px=100,
        blank_frame_path="missing.png",
    )

    result = detect_object(frame, config)

    assert result.detected is False


def test_detect_object_can_select_center_contour() -> None:
    frame = np.zeros((160, 200, 3), dtype=np.uint8)
    cv2.rectangle(frame, (5, 5), (70, 70), (255, 255, 255), -1)
    cv2.rectangle(frame, (92, 72), (107, 87), (255, 255, 255), -1)
    config = IOBoxSizerDetectionConfig(
        roi_x=0,
        roi_y=0,
        roi_width=200,
        roi_height=160,
        min_area_px=50,
        contour_selection="center",
    )

    result = detect_object(frame, config)

    assert result.detected is True
    assert result.center_x == 100
    assert result.center_y == 80


def test_detect_circle_object_reports_enclosing_diameter() -> None:
    frame = np.zeros((120, 120, 3), dtype=np.uint8)
    cv2.circle(frame, (60, 60), 20, (255, 255, 255), -1)
    config = IOBoxSizerDetectionConfig(
        mode="circle",
        roi_x=0,
        roi_y=0,
        roi_width=120,
        roi_height=120,
        min_area_px=100,
    )

    result = detect_object(frame, config)

    assert result.detected is True
    assert result.width_px == result.height_px
    assert 38 <= (result.width_px or 0) <= 42
