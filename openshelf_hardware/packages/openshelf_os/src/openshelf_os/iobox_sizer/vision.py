from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from openshelf_os.config.models import IOBoxSizerDetectionConfig


@dataclass(frozen=True)
class DetectionResult:
    detected: bool
    width_px: int | None = None
    height_px: int | None = None
    center_x: float | None = None
    center_y: float | None = None
    bbox_x: int | None = None
    bbox_y: int | None = None
    area_px: float | None = None


def _clip_roi(frame: np.ndarray, config: IOBoxSizerDetectionConfig) -> tuple[np.ndarray, int, int]:
    frame_height, frame_width = frame.shape[:2]
    x1 = min(max(config.roi_x, 0), frame_width)
    y1 = min(max(config.roi_y, 0), frame_height)
    x2 = min(x1 + config.roi_width, frame_width)
    y2 = min(y1 + config.roi_height, frame_height)

    if x2 <= x1 or y2 <= y1:
        return frame[0:0, 0:0], x1, y1

    return frame[y1:y2, x1:x2], x1, y1


def _load_blank_frame(
    config: IOBoxSizerDetectionConfig, shape: tuple[int, ...]
) -> np.ndarray | None:
    path = Path(config.blank_frame_path)
    if path.exists() is False:
        return None

    blank = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if blank is None or blank.shape[:2] != shape[:2]:
        return None

    return blank


def _diff_mask(
    roi: np.ndarray,
    frame_shape: tuple[int, ...],
    offset_x: int,
    offset_y: int,
    config: IOBoxSizerDetectionConfig,
    *,
    require_blank: bool = False,
) -> np.ndarray | None:
    blank = _load_blank_frame(config, frame_shape)
    if blank is None:
        if require_blank is True:
            return None

        return np.zeros(roi.shape[:2], dtype=np.uint8)

    blank_roi = blank[offset_y : offset_y + roi.shape[0], offset_x : offset_x + roi.shape[1]]
    diff = cv2.absdiff(roi, blank_roi)
    gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray_diff, config.diff_threshold, 255, cv2.THRESH_BINARY)
    return mask


def _brightness_mask(
    roi: np.ndarray,
    frame_shape: tuple[int, ...],
    offset_x: int,
    offset_y: int,
    config: IOBoxSizerDetectionConfig,
) -> np.ndarray:
    diff_mask = _diff_mask(roi, frame_shape, offset_x, offset_y, config)
    if diff_mask is None:
        diff_mask = np.zeros(roi.shape[:2], dtype=np.uint8)

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hsv_mask = cv2.inRange(
        hsv,
        np.array([0, 0, config.hsv_v_min], dtype=np.uint8),
        np.array([179, config.hsv_s_max, 255], dtype=np.uint8),
    )

    rgb_min = np.array(config.white_rgb_min, dtype=np.uint8)
    bgr_min = rgb_min[::-1]
    white_mask = cv2.inRange(roi, bgr_min, np.array([255, 255, 255], dtype=np.uint8))
    return cv2.bitwise_or(cv2.bitwise_or(hsv_mask, white_mask), diff_mask)


def _clean_mask(mask: np.ndarray) -> np.ndarray:
    kernel = np.ones((5, 5), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)


def _select_contour(
    contours: tuple[np.ndarray, ...],
    roi_shape: tuple[int, ...],
    config: IOBoxSizerDetectionConfig,
) -> tuple[np.ndarray, float] | None:
    candidates = [
        (float(cv2.contourArea(contour)), contour)
        for contour in contours
        if float(cv2.contourArea(contour)) >= config.min_area_px
    ]
    if not candidates:
        return None

    if config.contour_selection == "center":
        roi_height, roi_width = roi_shape[:2]
        target_x = roi_width / 2
        target_y = roi_height / 2

        def center_distance(item: tuple[float, np.ndarray]) -> tuple[float, float]:
            area, contour = item
            x, y, width, height = cv2.boundingRect(contour)
            center_x = x + width / 2
            center_y = y + height / 2
            distance = ((center_x - target_x) ** 2 + (center_y - target_y) ** 2) ** 0.5
            return distance, -area

        area, contour = min(candidates, key=center_distance)
        return contour, area

    area, contour = max(candidates, key=lambda item: item[0])
    return contour, area


def _detect_from_mask(
    frame: np.ndarray,
    config: IOBoxSizerDetectionConfig,
    mask: np.ndarray | None,
    *,
    use_circle: bool = False,
) -> DetectionResult:
    roi, offset_x, offset_y = _clip_roi(frame, config)
    if roi.size == 0 or mask is None:
        return DetectionResult(detected=False)

    mask = _clean_mask(mask)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    selected = _select_contour(contours, roi.shape, config)
    if selected is None:
        return DetectionResult(detected=False)

    contour, area = selected
    if use_circle is True:
        (center_x, center_y), radius = cv2.minEnclosingCircle(contour)
        diameter = max(1, int(round(radius * 2)))
        return DetectionResult(
            detected=True,
            width_px=diameter,
            height_px=diameter,
            center_x=float(offset_x + center_x),
            center_y=float(offset_y + center_y),
            bbox_x=offset_x + int(round(center_x - radius)),
            bbox_y=offset_y + int(round(center_y - radius)),
            area_px=area,
        )

    x, y, width, height = cv2.boundingRect(contour)
    return DetectionResult(
        detected=True,
        width_px=int(width),
        height_px=int(height),
        center_x=float(offset_x + x + width / 2),
        center_y=float(offset_y + y + height / 2),
        bbox_x=offset_x + int(x),
        bbox_y=offset_y + int(y),
        area_px=area,
    )


def detect_bright_object(frame: np.ndarray, config: IOBoxSizerDetectionConfig) -> DetectionResult:
    roi, offset_x, offset_y = _clip_roi(frame, config)
    if roi.size == 0:
        return DetectionResult(detected=False)

    mask = _brightness_mask(roi, frame.shape, offset_x, offset_y, config)
    return _detect_from_mask(frame, config, mask)


def detect_difference_object(
    frame: np.ndarray, config: IOBoxSizerDetectionConfig
) -> DetectionResult:
    roi, offset_x, offset_y = _clip_roi(frame, config)
    if roi.size == 0:
        return DetectionResult(detected=False)

    mask = _diff_mask(roi, frame.shape, offset_x, offset_y, config, require_blank=True)
    return _detect_from_mask(frame, config, mask)


def detect_circle_object(frame: np.ndarray, config: IOBoxSizerDetectionConfig) -> DetectionResult:
    roi, offset_x, offset_y = _clip_roi(frame, config)
    if roi.size == 0:
        return DetectionResult(detected=False)

    mask = _diff_mask(roi, frame.shape, offset_x, offset_y, config, require_blank=True)
    if mask is None:
        mask = _brightness_mask(roi, frame.shape, offset_x, offset_y, config)

    return _detect_from_mask(frame, config, mask, use_circle=True)


def detect_rectangle(frame: np.ndarray, config: IOBoxSizerDetectionConfig) -> DetectionResult:
    roi, offset_x, offset_y = _clip_roi(frame, config)
    if roi.size == 0:
        return DetectionResult(detected=False)

    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < config.min_area_px:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) == 4:
            candidates.append((area, contour))

    if not candidates:
        return DetectionResult(detected=False)

    _, contour = max(candidates, key=lambda item: item[0])
    x, y, width, height = cv2.boundingRect(contour)
    return DetectionResult(
        detected=True,
        width_px=int(width),
        height_px=int(height),
        center_x=float(offset_x + x + width / 2),
        center_y=float(offset_y + y + height / 2),
        bbox_x=offset_x + int(x),
        bbox_y=offset_y + int(y),
        area_px=float(cv2.contourArea(contour)),
    )


def detect_object(frame: np.ndarray, config: IOBoxSizerDetectionConfig) -> DetectionResult:
    if config.mode == "rectangle":
        return detect_rectangle(frame, config)

    if config.mode == "difference":
        return detect_difference_object(frame, config)

    if config.mode == "circle":
        return detect_circle_object(frame, config)

    return detect_bright_object(frame, config)
