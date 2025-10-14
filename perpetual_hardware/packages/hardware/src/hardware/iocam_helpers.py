from typing import Optional, Tuple
import cv2
import numpy as np

# ============================ Helpers (standalone) ============================


def _odd(n: int) -> int:
    return n if n % 2 == 1 else n + 1

def compute_fx_fy_from_fov(width_px: int, height_px: int,
                           hfov_deg: Optional[float], vfov_deg: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    fx = fy = None
    if hfov_deg is not None:
        fx = (width_px / 2.0) / np.tan(np.deg2rad(hfov_deg) / 2.0)
    if vfov_deg is not None:
        fy = (height_px / 2.0) / np.tan(np.deg2rad(vfov_deg) / 2.0)
    if fx is not None and fy is None:
        fy = fx * (height_px / width_px)
    if fy is not None and fx is None:
        fx = fy * (width_px / height_px)
    return fx, fy

def footprint_from_rect_pinhole(w_px: float, h_px: float,
                                z_mm: float, fx: float, fy: float) -> Tuple[float, float]:
    w_mm = (w_px * float(z_mm)) / float(fx)
    h_mm = (h_px * float(z_mm)) / float(fy)
    return w_mm, h_mm

from typing import Dict, Any, Tuple, List, Union

def detect_checkerboard_scale_detailed(gray: np.ndarray, square_mm: float) -> Optional[Dict[str, Union[Tuple[int, int], np.ndarray, float]]]:
    """
    Returns: {
      'pattern': (w,h),
      'corners': (N,1,2) float32,
      'px_per_mm_h': float,
      'px_per_mm_v': float,
      'px_per_mm'  : float,
      'center_xy'  : (int,int)
    }
    """
    CANDIDATE_PATTERNS = [(25, 20)]  # add more if you use different boards
    for pattern in CANDIDATE_PATTERNS:
        found, corners = cv2.findChessboardCorners(
            gray, pattern,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_FAST_CHECK
        )
        if not found:
            continue
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3)
        corners = cv2.cornerSubPix(gray, corners, (5, 5), (-1, -1), criteria)

        w, h = pattern
        c2 = corners.reshape(-1, 2)

        horiz, vert = [], []
        for r in range(h):
            for c in range(w - 1):
                i, j = r*w + c, r*w + (c+1)
                horiz.append(np.linalg.norm(c2[j] - c2[i]))
        for r in range(h - 1):
            for c in range(w):
                i, j = r*w + c, (r+1)*w + c
                vert.append(np.linalg.norm(c2[j] - c2[i]))

        if not (horiz and vert):
            continue

        med_h = float(np.median(horiz))
        med_v = float(np.median(vert))
        all_spacings = horiz + vert
        med_all = float(np.median(all_spacings))

        px_per_mm_h = med_h / square_mm
        px_per_mm_v = med_v / square_mm
        px_per_mm = med_all / square_mm

        cx, cy = np.mean(c2, axis=0)
        center_xy = (int(round(cx)), int(round(cy)))
        return {
            "pattern": pattern,
            "corners": corners,
            "px_per_mm_h": px_per_mm_h,
            "px_per_mm_v": px_per_mm_v,
            "px_per_mm": px_per_mm,
            "center_xy": center_xy,
        }
    return None

def segment_object_on_checkerboard(gray: np.ndarray, px_per_mm: float, square_mm: float) -> np.ndarray:
    square_px = max(6, int(round(px_per_mm * square_mm)))
    k_blur = int(np.clip(square_px * 3, 9, 151)); k_blur = _odd(k_blur)
    blur = cv2.GaussianBlur(gray, (k_blur, k_blur), 0)

    block = int(np.clip(square_px * 3 + 1, 15, 255)); block = _odd(block)
    th = cv2.adaptiveThreshold(
        blur, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
        block, 2
    )

    k_open = int(np.clip(square_px * 0.5, 3, 41)); k_open = _odd(k_open)
    k_close = int(np.clip(square_px * 0.5, 5, 9)); k_close = _odd(k_close)
    open_kernel  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_open, k_open))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_close, k_close))
    mask = cv2.morphologyEx(th, cv2.MORPH_OPEN, open_kernel, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel, iterations=1)

    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num_labels > 1:
        h, w = gray.shape
        min_px = int(0.01 * h * w)
        keep = np.zeros_like(mask)
        for i in range(1, num_labels):
            if stats[i, cv2.CC_STAT_AREA] >= min_px:
                keep[labels == i] = 255
        mask = keep
    return mask

def pick_main_contour(mask: np.ndarray) -> Optional[np.ndarray]:
    h, w = mask.shape
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid_cnts = []
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if area < 50:
            continue
        if cw/ch > 5 or ch/cw > 5:
            continue
        if x <= 0 or y <= 0 or (x + cw) >= w or (y + ch) >= h:
            continue
        valid_cnts.append((area, c))
    if not valid_cnts:
        return None
    valid_cnts.sort(key=lambda x: x[0], reverse=True)
    return valid_cnts[1][1] if len(valid_cnts) > 1 else valid_cnts[0][1]

def measure_contour_px(contour: np.ndarray) -> Tuple[float, float, np.ndarray, Tuple[int, int]]:
    rect = cv2.minAreaRect(contour)
    (cx, cy), (w_px, h_px), _angle = rect
    box = np.int32(np.round(cv2.boxPoints(rect)))  # shape (4,2)
    return float(w_px), float(h_px), box, (int(round(cx)), int(round(cy)))