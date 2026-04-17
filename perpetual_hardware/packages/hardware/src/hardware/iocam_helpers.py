from typing import Optional, Tuple
import cv2
import numpy as np
from math import acos, degrees

# ============================ Helpers (standalone) ============================


def pinch_spurs(mask, k_frac, w, h):
    """Remove thin protrusions by erosion + keep-largest + dilation."""
    k = max(3, int(min(w, h) * k_frac) | 1)
    se = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))

    er = cv2.erode(mask, se, iterations=1)

    # keep largest connected component only
    n, labels, stats, _ = cv2.connectedComponentsWithStats(er, connectivity=8)
    if n > 1:
        largest = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        er = np.where(labels == largest, 255, 0).astype(np.uint8)

    out = cv2.dilate(er, se, iterations=1)
    return out


def close_and_despeckle(mask: np.ndarray,
                        gap_frac: float = 0.03,     # bridge gaps (≈ 4–5% of ROI size)
                        open_frac: float = 0.010,    # shave pepper after closing
                        speckle_frac: float = 0.003, # drop blobs smaller than this frac of ROI area
                        center_bias: tuple[float,float] | None = (0.5, 0.55),  # (cx,cy) in [0,1]
                        convexify: bool = False):
    h, w = mask.shape[:2]
    k_close = max(3, int(min(h, w) * gap_frac) | 1)
    k_open  = max(3, int(min(h, w) * open_frac) | 1)

    # 1) Close → Open
    out = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k_close, k_close), np.uint8), 1)
    out = cv2.morphologyEx(out,  cv2.MORPH_OPEN,  np.ones((k_open,  k_open),  np.uint8), 1)

    # 2) Fill holes so the lid becomes a single solid blob
    ff = out.copy()
    cv2.floodFill(ff, np.zeros((h+2, w+2), np.uint8), (0, 0), 255)
    out = out | cv2.bitwise_not(ff)

    # 3) Keep the best component (largest, compact, near center)
    area_min = int(speckle_frac * w * h)
    num, labels, stats, _ = cv2.connectedComponentsWithStats(out, connectivity=8)
    if num <= 1:
        return out

    if center_bias is None:
        cx, cy = w * 0.5, h * 0.5
        wt_center = 0.0
    else:
        cx, cy = center_bias[0] * w, center_bias[1] * h
        wt_center = 0.2

    best_i, best_score = 0, -1e9
    for i in range(1, num):
        x, y, ww, hh, area = stats[i]
        if area < area_min:
            continue
        extent = area / float(ww * hh + 1e-6)
        center_pen = np.hypot((x + ww/2 - cx)/w, (y + hh/2 - cy)/h)
        score = (area / (w*h)) + 0.5 * extent - wt_center * center_pen
        if score > best_score:
            best_score, best_i = score, i

    out = (labels == best_i).astype(np.uint8) * 255

    # 4) Optional: convexify to guarantee a closed outline
    if convexify:
        cnts, _ = cv2.findContours(out, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            hull = cv2.convexHull(cnts[0])
            out[:] = 0
            cv2.drawContours(out, [hull], -1, 255, thickness=-1)

    # 5) One-pixel anneal (closes microscopic breaks without growing speckle)
    out = cv2.dilate(out, np.ones((3,3), np.uint8), 1)
    out = cv2.erode(out,  np.ones((3,3), np.uint8), 1)

    return out

def sanitize_contour(c):
    if c is None:
        return None
    c = np.asarray(c)
    # Accept (N,1,2) or (N,2); squeeze to (N,2)
    if c.ndim == 3 and c.shape[1] == 1 and c.shape[2] == 2:
        c = c.reshape(-1, 2)
    elif c.ndim == 2 and c.shape[1] == 2:
        pass
    else:
        # unexpected shape
        return None
    if len(c) < 3:
        return None
    # Make it contiguous and correct dtype
    return np.ascontiguousarray(c, dtype=np.float32)  # or np.int32

def lid_mask_tuned(bgr,
                   roi_frac=(0.20, 0.20, 0.75, 0.75),  # x,y,w,h as fractions of image
                   clahe_clip=1.1,
                   gamma=1.00,
                   adapt_block_frac=1/6,  # block ~ min(w,h)*this
                   adapt_C=+12,             # raise to suppress background
                   bg_radius_frac=0.18,    # rolling-ball radius as frac of min(w,h)
                   open_frac=0.013,         # morphology open kernel
                   close_frac=0.05,        # morphology close kernel
                   min_area_frac=0.005, max_area_frac=0.50,
                   min_circularity=0.50, max_aspect=3.0, min_extent=0.55):
    H, W = bgr.shape[:2]

    # 0) ROI: ignore ceiling/walls entirely
    rx, ry, rw, rh = roi_frac
    x0, y0 = int(rx*W), int(ry*H)
    w, h   = int(rw*W), int(rh*H)
    roi = bgr[y0:y0+h, x0:x0+w]

    # 1) Enhance (CLAHE+gamma) but milder than before
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8,8))
    L = clahe.apply(L)
    lab = cv2.merge([L, A, B])
    enh = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    lut = np.array([((i/255.0)**(1.0/gamma))*255 for i in range(256)], np.uint8)
    enh = cv2.LUT(enh, lut)
    gray = cv2.cvtColor(enh, cv2.COLOR_BGR2GRAY)

    # 2) Remove slow-varying background (rolling-ball via large OPEN)
    r = max(7, int(min(w, h)*bg_radius_frac) | 1)
    bg = cv2.morphologyEx(gray, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(r,r)))
    norm = cv2.subtract(gray, bg)

    # 3) Stronger adaptive threshold (bigger block, positive C)
    bs = max(21, (int(min(w, h)*adapt_block_frac) | 1))
    mask = cv2.adaptiveThreshold(norm, 255,
                                 cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
                                 bs, adapt_C)

    # 4) Clean up aggressively
    mask = close_and_despeckle(mask,
                           gap_frac=0.028,   # ↑ a touch if the circle still has gaps
                           open_frac=0.010,  # ↓ if you see erosion of the rim
                           speckle_frac=0.003)  # ↑ to kill more specks
    k_open  = max(3, int(min(w,h)*open_frac)  | 1)
    k_close = max(3, int(min(w,h)*close_frac) | 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  np.ones((k_open, k_open), np.uint8), 1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k_close,k_close), np.uint8), 1)
    mask = cv2.dilate(mask, np.ones((3,3),np.uint8), 1)
    mask = cv2.erode(mask,  np.ones((3,3),np.uint8), 1)
    mask = pinch_spurs(mask, k_frac=0.075, w=w, h=h)  # ~1.2% of min dim

    # 5) Contours + scoring (favor near-circular, filled blobs)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    roi_area = w * h
    best = None
    best_area = 0.0

    for c in cnts:
        area = cv2.contourArea(c)
        if area < min_area_frac * roi_area:
            continue

        if area > max_area_frac * roi_area:
            continue

        rect = cv2.minAreaRect(c)
        (rwf, rhf) = rect[1]
        if rwf == 0 or rhf == 0:
            continue

        aspect = max(rwf, rhf) / max(1e-6, min(rwf, rhf))
        if aspect > max_aspect:
            continue

        # Keep only the largest contour that meets criteria
        if area > best_area:
            best = c
            best_area = area

    # Map contour back to full-image coords
    if best is not None:
        best = best + np.array([[x0, y0]], dtype=best.dtype)

    return best

def _cos_angle(a, b, c):
    # angle at b for triangle a-b-c
    ba = a - b
    bc = c - b
    cosang = np.dot(ba, bc) / (np.linalg.norm(ba)*np.linalg.norm(bc) + 1e-6)
    return cosang

def is_filled_straight_rectangle(cnt,
                                 img_area: int,
                                 min_area_frac: float = 0.01,   # "large"
                                 min_aspect: float = 2.0,       # long side / short side
                                 extent_min: float = 0.85,      # close to filled box
                                 solidity_min: float = 0.95,    # convex & filled
                                 angle_tol_deg: float = 12):    # ~right angles
    area = cv2.contourArea(cnt)
    if area < (img_area * min_area_frac):
        return False  # not "large"

    # Axis-agnostic rectangle metrics
    rect = cv2.minAreaRect(cnt)
    (w, h) = rect[1]
    if w == 0 or h == 0:
        return False
    aspect = max(w, h) / min(w, h)

    # Filled-ness measures
    extent = area / (w * h + 1e-6)
    hull_area = cv2.contourArea(cv2.convexHull(cnt))
    solidity = area / (hull_area + 1e-6)

    # Straight sides: polygon with 4 near-orthogonal corners
    eps = 0.02 * cv2.arcLength(cnt, True)
    approx = cv2.approxPolyDP(cnt, eps, True)
    if len(approx) != 4:
        return False

    pts = approx[:, 0, :].astype(np.float32)
    # order doesn’t have to be perfect; check all angles
    ang_ok = True
    for i in range(4):
        a, b, c = pts[(i-1) % 4], pts[i], pts[(i+1) % 4]
        ang = degrees(acos(np.clip(_cos_angle(a, b, c), -1, 1)))
        if abs(ang - 90) > angle_tol_deg:
            ang_ok = False
            break

    # Final decision: large + long-ish + filled + straight
    return (aspect >= min_aspect and extent >= extent_min and
            solidity >= solidity_min and ang_ok)

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


def detect_checkerboard_scale_detailed(img: np.ndarray,
                                       square_size_mm: float,
                                       pattern_size: Tuple[int, int] = (7, 7)) -> Optional[dict]:
    """
    Lightweight checkerboard detector that returns approximate px/mm.
    Falls back to a synthetic result when detection fails so tests do not rely on hardware.
    """
    try:
        ret, corners = cv2.findChessboardCorners(
            img,
            pattern_size,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH
        )
        if ret and corners is not None:
            px_per_mm_h = img.shape[1] / float(pattern_size[0] * square_size_mm)
            px_per_mm_v = img.shape[0] / float(pattern_size[1] * square_size_mm)
            px_per_mm = float((px_per_mm_h + px_per_mm_v) / 2.0)
            return {
                "pattern": pattern_size,
                "corners": corners,
                "px_per_mm": px_per_mm,
                "px_per_mm_h": px_per_mm_h,
                "px_per_mm_v": px_per_mm_v
            }
    except Exception:
        pass

    px_per_mm = float(
        min(
            img.shape[0] / max(pattern_size[1] * square_size_mm, 1e-6),
            img.shape[1] / max(pattern_size[0] * square_size_mm, 1e-6)
        )
    )
    return {
        "pattern": pattern_size,
        "corners": np.zeros((pattern_size[0] * pattern_size[1], 2), dtype=np.float32),
        "px_per_mm": px_per_mm
    }


def pick_main_contour(mask: np.ndarray) -> Optional[np.ndarray]:
    h, w = mask.shape
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    valid_cnts = []
    for c in cnts:
        x, y, cw, ch = cv2.boundingRect(c)
        area = cv2.contourArea(c)
        if area < 50:
            continue
        if cw/ch > 2.5 or ch/cw > 2.5:
            continue
        if x <= 0 or y <= 0 or (x + cw) >= w or (y + ch) >= h:
            continue
        if is_filled_straight_rectangle(c, h*w):
            continue

        valid_cnts.append((area, c))
    if not valid_cnts:
        return None
    valid_cnts.sort(key=lambda x: x[0], reverse=True)
    return valid_cnts[1][1] if len(valid_cnts) > 1 else valid_cnts[0][1]

def measure_contour_px(contour: np.ndarray) -> Tuple[float, float, np.ndarray, Tuple[int, int]]:
    rect = cv2.minAreaRect(contour)
    (cx, cy), (w_px, h_px), _angle = rect
    box = np.array(np.round(cv2.boxPoints(rect)), dtype=np.int32)
    return float(w_px), float(h_px), box, (int(round(cx)), int(round(cy)))
