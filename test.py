import asyncio
import os
import time
from typing import Optional, Tuple

import cv2
import numpy as np
import ArducamDepthCamera as ac

# ============================ Helpers ============================

def _odd(n: int) -> int:
    return n if n % 2 == 1 else n + 1

def _put_text(img, text, org, scale=0.55, color=(255, 255, 255)):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)

def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

def _ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def detect_checkerboard_scale_detailed(gray: np.ndarray, square_mm: float) -> Optional[float]:
    """Returns px_per_mm if found"""
    CANDIDATE_PATTERNS = [(25, 20)]
    for pattern in CANDIDATE_PATTERNS:
        found, corners = cv2.findChessboardCorners(gray, pattern, flags=cv2.CALIB_CB_FAST_CHECK)
        if not found:
            continue
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
        med_all = float(np.median(horiz + vert))
        return med_all / square_mm
    return None

def segment_object_on_checkerboard(gray: np.ndarray, px_per_mm: float, square_mm: float) -> np.ndarray:
    square_px = max(6, int(round(px_per_mm * square_mm)))
    k_blur = _odd(int(np.clip(square_px * 3, 9, 151)))
    blur = cv2.GaussianBlur(gray, (k_blur, k_blur), 0)
    block = _odd(int(np.clip(square_px * 3 + 1, 15, 255)))
    th = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, block, 2)
    k_open = _odd(int(np.clip(square_px * 0.5, 3, 41)))
    k_close = _odd(int(np.clip(square_px * 0.5, 5, 9)))
    open_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_open, k_open))
    close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_close, k_close))
    mask = cv2.morphologyEx(th, cv2.MORPH_OPEN, open_kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel)
    return mask

def pick_main_contour(mask: np.ndarray):
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnts = sorted(cnts, key=cv2.contourArea, reverse=True)
    return cnts[0]

# ---------- Stabilized, axis-aligned rectangle (one edge horizontal) ----------

class _RectEMA:
    """Simple EMA smoother for center and size."""
    def __init__(self, alpha: float = 0.5):
        self.alpha = alpha
        self.cx = None
        self.cy = None
        self.w = None
        self.h = None

    def update(self, cx, cy, w, h):
        if self.cx is None:
            self.cx, self.cy, self.w, self.h = float(cx), float(cy), float(w), float(h)
        else:
            a = self.alpha
            self.cx = a * float(cx) + (1 - a) * self.cx
            self.cy = a * float(cy) + (1 - a) * self.cy
            self.w  = a * float(w)  + (1 - a) * self.w
            self.h  = a * float(h)  + (1 - a) * self.h
        return self.cx, self.cy, self.w, self.h

def measure_stabilized_axis_aligned(contour: np.ndarray, ema: _RectEMA):
    """
    Returns (w_px, h_px, box_points_int, (cx, cy)) for a rectangle whose edges are
    parallel to the image axes (top edge of frame). Uses minAreaRect for size/center,
    normalizes width>=height, then EMA-smooths center/size to reduce jitter.
    """
    rect = cv2.minAreaRect(contour)
    (cx, cy), (w_px, h_px), angle = rect  # angle in [-90,0)
    # Normalize so width is the longer side; avoid 90° flips frame-to-frame
    if w_px < h_px:
        w_px, h_px = h_px, w_px
        # angle += 90  # not needed since we will snap to 0°
    # Snap angle to 0°: keep the size, make the box axis-aligned around the same center
    cx_s, cy_s, w_s, h_s = ema.update(cx, cy, w_px, h_px)
    snapped_rect = ((cx_s, cy_s), (w_s, h_s), 0.0)
    box = cv2.boxPoints(snapped_rect)
    box = np.int32(np.round(box))
    return float(w_s), float(h_s), box, (int(round(cx_s)), int(round(cy_s)))

# ============================ Async Live Sizer ============================

class AsyncFootprintSizer:
    def __init__(self, *, usb_index=0,
                 square_mm=7.5, headless_fallback_path="./usb_sizing.jpg"):
        self._usb_index = usb_index
        self.square_mm = square_mm
        self._usb = None
        self._px_per_mm = None
        self._headless_out = headless_fallback_path
        self._rect_ema = _RectEMA(alpha=0.5)  # smoothing for stable display

    async def start(self):
        self._usb = cv2.VideoCapture(self._usb_index, cv2.CAP_V4L2)
        if not self._usb.isOpened():
            raise RuntimeError(f"Cannot open USB camera {self._usb_index}")

    async def stop(self):
        if self._usb:
            self._usb.release()
            self._usb = None

    async def run_live(self, bounding_box: Tuple[int, int, int, int], max_center_offset_px=50):
        loop = asyncio.get_running_loop()
        bbox = bounding_box
        while True:
            ok, fr = await loop.run_in_executor(None, self._usb.read)
            if not ok:
                await asyncio.sleep(0.01)
                continue

            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
            if self._px_per_mm is None:
                self._px_per_mm = detect_checkerboard_scale_detailed(gray, self.square_mm) or 2.5

            mask = segment_object_on_checkerboard(gray, self._px_per_mm, self.square_mm)
            contour = pick_main_contour(mask)
            draw = fr.copy()

            if contour is not None:
                # Draw raw contour for reference
                cv2.drawContours(draw, [contour], -1, (0, 200, 0), 2)

                # Stabilized, axis-aligned rectangle
                w_px, h_px, box, (cx, cy) = measure_stabilized_axis_aligned(contour, self._rect_ema)

                # Box and center
                cv2.polylines(draw, [box], True, (255, 0, 0), 2)
                cv2.circle(draw, (cx, cy), 4, (0, 255, 255), -1)

                # Bounding-box center distance overlay
                x0, y0, x1, y1 = bbox
                bx_cx, by_cy = (x0 + x1) / 2, (y0 + y1) / 2
                cv2.circle(draw, (int(bx_cx), int(by_cy)), 4, (200, 200, 200), -1)
                dist_px = float(np.hypot(cx - bx_cx, cy - by_cy))
                dist_mm = dist_px / self._px_per_mm if self._px_per_mm else None
                cv2.line(draw, (int(bx_cx), int(by_cy)), (int(cx), int(cy)), (0, 0, 255), 2)
                mx, my = int((bx_cx + cx) / 2), int((by_cy + cy) / 2)
                if dist_mm is not None:
                    _put_text(draw, f"{dist_px:.1f}px / {dist_mm:.1f}mm", (mx + 10, my - 8), 0.6)
                else:
                    _put_text(draw, f"{dist_px:.1f}px", (mx + 10, my - 8), 0.6)

                # Size readout
                w_mm = w_px / self._px_per_mm
                h_mm = h_px / self._px_per_mm
                _put_text(draw, f"W×H: {w_px:.1f}px × {h_px:.1f}px  ({w_mm:.1f}mm × {h_mm:.1f}mm)", (10, 50), 0.6)

            _put_text(draw, f"px_per_mm={self._px_per_mm:.3f}", (10, 25), 0.6)
            self._safe_show(draw, "usb_live")

            # Break on 'q'
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    def _safe_show(self, frame, window_name):
        if _has_display():
            try:
                cv2.imshow(window_name, frame)
                return
            except Exception as e:
                print(f"[display] failed: {e}")
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = f"{os.path.splitext(self._headless_out)[0]}-{ts}.jpg"
        _ensure_parent_dir(out_path)
        cv2.imwrite(out_path, frame)
        print(f"[display] wrote {out_path}")

# ============================ Runner ============================

if __name__ == "__main__":
    async def _main():
        sizer = AsyncFootprintSizer(usb_index=0)
        await sizer.start()
        try:
            bbox = (200, 60, 408, 265)
            await sizer.run_live(bbox)
        finally:
            await sizer.stop()
            cv2.destroyAllWindows()

    asyncio.run(_main())
