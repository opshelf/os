from typing import Optional, Tuple
import cv2
import numpy as np
from math import acos, degrees, pi

# ============================ Helpers (standalone) ============================
import cv2, numpy as np
from math import pi

import cv2
import numpy as np

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
                   roi_frac=(0.25, 0.25, 0.5, 0.5),  # x,y,w,h as fractions of image
                   clahe_clip=2.0,
                   gamma=1.05,
                   adapt_block_frac=1/6,  # block ~ min(w,h)*this
                   adapt_C=+9,             # raise to suppress background
                   bg_radius_frac=0.10,    # rolling-ball radius as frac of min(w,h)
                   open_frac=0.015,         # morphology open kernel
                   close_frac=0.035,        # morphology close kernel
                   min_area_frac=0.006, max_area_frac=0.25,
                   min_circularity=0.50, max_aspect=2.0, min_extent=0.55,
                   return_debug=False):
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


    # cv2.imwrite("mask.png",mask)

    # 5) Contours + scoring (favor near-circular, filled blobs)
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    roi_area = w * h
    best = None
    best_area = 0

    for c in cnts:
        area = cv2.contourArea(c)
        if area < min_area_frac * roi_area:
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

    # if return_debug:
    #     # lift mask back to full image for visualization
    #     full_mask = np.zeros((H, W), np.uint8)
    #     full_mask[y0:y0+h, x0:x0+w] = mask
    #     dbg = dict(bs=bs, r=r, open=k_open, close=k_close, score=float(score))
    #     return best, full_mask, dbg
    return best


def detect_lid_contour(
    bgr: np.ndarray,
    roi: tuple[int,int,int,int] | None = None,   # (x,y,w,h) crop; None = whole frame
    min_circularity: float = 0.65,
    max_aspect: float = 2.0,
    min_extent: float = 0.55,
    area_frac_range: tuple[float,float] = (0.002, 0.35),  # as fraction of ROI area
    return_debug: bool = False,
    ):
        """
        Returns:
            best_cnt: np.ndarray | None  (contour in *full-image* coordinates)
            (mask, debug): optional tuple if return_debug=True
        """
        if bgr is None or bgr.size == 0:
            return (None, (None, {})) if return_debug else None

        H, W = bgr.shape[:2]
        x0, y0, w, h = (0, 0, W, H) if roi is None else roi
        # Clamp ROI into image
        x0 = max(0, min(x0, W-1)); y0 = max(0, min(y0, H-1))
        w  = max(1, min(w, W - x0)); h = max(1, min(h, H - y0))

        img = bgr[y0:y0+h, x0:x0+w].copy()

        # ---------- 1) Enhance (CLAHE on L + gentle gamma lift) ----------
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        L, A, B = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
        L = clahe.apply(L)
        lab = cv2.merge([L, A, B])
        enh = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        gamma = 1.25
        lut = np.array([((i/255.0) ** (1.0/gamma)) * 255 for i in range(256)], dtype=np.uint8)
        enh = cv2.LUT(enh, lut)

        gray = cv2.cvtColor(enh, cv2.COLOR_BGR2GRAY)

        # ---------- 2) Illumination-robust threshold ----------
        # Pick a reasonable odd block size from image scale
        bs = max(21, int(min(w, h) / 20) | 1)     # ~1/20 of ROI, forced odd
        mask_adapt = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, bs, -5
        )

        # ---------- 3) Edge reinforcement ----------
        med = np.median(gray)
        lo = int(max(5, 0.66 * med))
        hi = int(min(255, 1.33 * med + 20))
        edges = cv2.Canny(gray, lo, hi)
        edges = cv2.dilate(edges, np.ones((3,3), np.uint8), iterations=1)

        mask = cv2.bitwise_or(mask_adapt, edges)

        # ---------- 4) Morph cleanup ----------
        k_close = max(3, int(min(w, h) * 0.01))  # ~1% of ROI
        k_open  = max(3, int(min(w, h) * 0.008))
        k_close |= 1; k_open |= 1
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k_close, k_close), np.uint8), 1)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  np.ones((k_open,  k_open),  np.uint8), 1)

        # cv2.imwrite("mask.png", mask)

        # ---------- 5) Contours & scoring ----------
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        roi_area = float(w * h)
        best_cnt, best_score = None, -1e9

        cx_roi, cy_roi = w * 0.5, h * 0.45  # bias slightly upper center (your scene)
        center_wt = 0.10                    # how much to prefer center placement

        for c in cnts:
            area = cv2.contourArea(c)
            if area <= 1:
                continue
            frac = area / roi_area
            if not (area_frac_range[0] <= frac <= area_frac_range[1]):
                continue

            perim = cv2.arcLength(c, True)
            if perim <= 1:
                continue
            circularity = 4.0 * pi * area / (perim * perim + 1e-6)

            rect = cv2.minAreaRect(c)
            (rw, rh) = rect[1]
            if rw == 0 or rh == 0:
                continue
            aspect = max(rw, rh) / min(rw, rh)

            extent = area / (rw * rh + 1e-6)

            if circularity < min_circularity or aspect > max_aspect or extent < min_extent:
                continue

            # # Prefer near-circular, filled blobs near center
            # M = cv2.moments(c)
            # if M["m00"] > 0:
            #     cx = M["m10"] / M["m00"]
            #     cy = M["m01"] / M["m00"]
            # else:
            #     cx, cy = rect[0]

            # # center penalty (normalized)
            # d = np.hypot((cx - cx_roi) / w, (cy - cy_roi) / h)

            # score = (
            #     1.0 * circularity
            #     + 0.25 * extent
            #     - 0.20 * abs(aspect - 1.0)
            #     - center_wt * d
            # )

            # if score > best_score:
            #     best_score, best_cnt = score, c

        # Map contour back to full-image coords if ROI used
        if best_cnt is not None and (x0 != 0 or y0 != 0):
            best_cnt = best_cnt + np.array([[x0, y0]], dtype=best_cnt.dtype)

        if return_debug:
            dbg = {
                "score": float(best_score),
                "params": {
                    "blockSize": bs, "min_circularity": min_circularity,
                    "max_aspect": max_aspect, "min_extent": min_extent,
                    "area_frac_range": area_frac_range
                },
                "roi": (int(x0), int(y0), int(w), int(h))
            }
            return best_cnt, (mask if roi is None else cv2.copyMakeBorder(
                mask, y0, H-(y0+h), x0, W-(x0+w), cv2.BORDER_CONSTANT, value=0
            ), dbg)

        return best_cnt

def enhance_for_pale_objects(bgr):
    # Convert to LAB
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)

    # CLAHE on the lightness channel
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
    L = clahe.apply(L)

    # Merge back and convert to BGR
    lab = cv2.merge([L, A, B])
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    # Gentle gamma lift to brighten midtones
    gamma = 1.3
    lut = np.array([((i/255.0)**(1/gamma))*255 for i in np.arange(256)]).astype("uint8")
    out = cv2.LUT(out, lut)

    return out

def segment_light_objects(bgr: np.ndarray) -> np.ndarray:
    # --- 1) White-balance (gray-world) + gentle gamma lift ---
    img = bgr.astype(np.float32) / 255.0
    gains = img.mean(axis=(0,1))
    gains = gains.mean() / (gains + 1e-6)
    img = np.clip(img * gains, 0, 1)
    gamma = 1.2  # lift midtones for pale objects
    img = np.clip(img ** (1.0/gamma), 0, 1)
    img8 = (img * 255).astype(np.uint8)

    # --- 2) Convert spaces ---
    lab = cv2.cvtColor(img8, cv2.COLOR_BGR2LAB)
    L, A, B = cv2.split(lab)
    hsv = cv2.cvtColor(img8, cv2.COLOR_BGR2HSV)
    H, S, V = cv2.split(hsv)

    # --- 3) Illumination-robust L threshold ---
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    Lc = clahe.apply(L)
    th_adapt = cv2.adaptiveThreshold(
        Lc, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 35, -5
    )

    # --- 4) Chroma cue (Lab chroma or HSV S) ---
    chroma = cv2.normalize(np.sqrt((A.astype(np.float32)-128)**2 + (B.astype(np.float32)-128)**2),
                           None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    _, th_chroma = cv2.threshold(chroma, 10, 255, cv2.THRESH_BINARY)  # small chroma still helps
    _, th_sat = cv2.threshold(S, 20, 255, cv2.THRESH_BINARY)

    # --- 5) Edges/gradient cue to catch pale low-contrast boundaries ---
    edges = cv2.Canny(Lc, 60, 120)
    edges = cv2.dilate(edges, np.ones((3,3), np.uint8), iterations=1)

    # --- 6) Fuse cues ---
    mask = th_adapt | (th_chroma | th_sat) | edges

    # --- 7) Clean up morphology ---
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5,5), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,  np.ones((3,3), np.uint8), iterations=1)

    return mask

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

    # cv2.imwrite("beb.png", mask)

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
    box = np.int32(np.round(cv2.boxPoints(rect)))  # shape (4,2)
    return float(w_px), float(h_px), box, (int(round(cx)), int(round(cy)))