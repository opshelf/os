import cv2
import numpy as np
from typing import Tuple, Optional
import cv2
from skimage.metrics import structural_similarity
import os


class Detector:
    def __init__(self, baseline_path: str = "baseline.jpg") -> None:
        self._baseline_path = baseline_path

    def save_baseline_image(self, frame: np.ndarray, path: str = "baseline.jpg") -> None:
        cv2.imwrite(path, frame)

    def get_baseline_image(self, path: str = "baseline.jpg") -> Optional[np.ndarray]:
        if os.path.exists(path):
            return cv2.imread(path)
        return None

    def prepare_image(self, frame: np.ndarray) -> np.ndarray:
        blurred_image = cv2.GaussianBlur(frame, (5, 5), 0)
        gray_image = cv2.cvtColor(blurred_image, cv2.COLOR_BGR2GRAY)
        _, thresholded_image = cv2.threshold(gray_image, 127, 255, cv2.THRESH_BINARY)
        return thresholded_image

    def item_present(
        self, baseline: np.ndarray, frame: np.ndarray
    ) -> Tuple[bool, float, np.ndarray]:
        prepared_frame = self.prepare_image(frame)
        prepared_baseline = self.prepare_image(baseline)
        score, _ = structural_similarity(prepared_baseline, prepared_frame, full=True)
        diff = 255 - cv2.absdiff(prepared_baseline, prepared_frame)
        kernel = np.ones((12, 12), np.uint8)
        closing = cv2.morphologyEx(diff, cv2.MORPH_CLOSE, kernel)
        # img = cv2.cvtColor(closing, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(closing, 100, 255, cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        MIN_CONTOUR_AREA = 10000  # Adjust based on your use case
        MAX_CONTOUR_AREA = 300000
        filtered_contours = [
            cnt for cnt in contours if MIN_CONTOUR_AREA < cv2.contourArea(cnt) < MAX_CONTOUR_AREA
        ]
        cv2.drawContours(frame, filtered_contours, -1, (0, 255, 0), 3)
        is_present = len(filtered_contours) > 0 or (score * 100) < 85.0
        return (is_present, score, frame)
