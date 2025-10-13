import asyncio
import cv2
import numpy as np
import ArducamDepthCamera as ac
from typing import Optional, Tuple, List, Dict, Any

# ----------------------------- Helpers -----------------------------

from typing import Sequence, Tuple, List

def corners_within_bbox(
    corners: np.ndarray,  # shape (4,2), dtype int/float
    bbox: Tuple[int, int, int, int],  # (x0, y0, x1, y1)
    inclusive: bool = True
) -> Tuple[bool, List[int]]:
    """
    Returns (all_inside, oob_indices).
    Corner order is whatever cv2.boxPoints returns (rotated rect).
    """
    x0, y0, x1, y1 = bbox
    oob = []
    for i, (x, y) in enumerate(corners):
        if inclusive:
            inside = (x0 <= x <= x1) and (y0 <= y <= y1)
        else:
            inside = (x0 <  x <  x1) and (y0 <  y <  y1)
        if not inside:
            oob.append(i)
    return (len(oob) == 0), oob


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

def detect_checkerboard_scale_detailed(gray: np.ndarray, square_mm: float) -> Optional[dict]:
    CANDIDATE_PATTERNS = [(25, 20)]
    for pattern in CANDIDATE_PATTERNS:
        found, corners = cv2.findChessboardCorners(
            gray, pattern,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_FAST_CHECK
        )
        if not found:
            continue
        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 1e-3)
        corners = cv2.cornerSubPix(gray, corners, (5,5), (-1,-1), criteria)

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

def measure_contour_px(contour: np.ndarray) -> Tuple[float, float, np.ndarray, Tuple[int,int]]:
    rect = cv2.minAreaRect(contour)
    (cx, cy), (w_px, h_px), angle = rect
    box = np.int32(np.round(cv2.boxPoints(rect)))
    return float(w_px), float(h_px), box, (int(round(cx)), int(round(cy)))

def _put_text(img, text, org, scale=0.55, color=(255,255,255)):
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2, cv2.LINE_AA)

# ----------------------------- Main Class -----------------------------

class AsyncFootprintSizer:
    """
    Async wrapper for TOF + USB sizing with optional on-screen visualization.
    - await start()
    - await measure(n_frames=5, display=True, window_name="usb_sizing")
    - await stop()
    """

    def __init__(
        self,
        *,
        preferred_tof_port: Optional[int] = None,
        max_distance_mm: int = 4000,
        usb_index: int = 0,
        usb_width: Optional[int] = None,
        usb_height: Optional[int] = None,
        usb_fourcc: str = "MJPG",
        usb_exposure: Optional[float] = None,
        square_mm: float = 7.5,
        fx_px: Optional[float] = 1150,
        fy_px: Optional[float] = 1150,
        hfov_deg: Optional[float] = None,
        vfov_deg: Optional[float] = None,
        confidence_min: int = 0
    ) -> None:
        self.preferred_tof_port = preferred_tof_port
        self.max_distance_mm = max_distance_mm
        self.square_mm = square_mm
        self.hfov_deg = hfov_deg
        self.vfov_deg = vfov_deg
        self.fx_px = fx_px
        self.fy_px = fy_px
        self.confidence_min = confidence_min

        self._usb_index = usb_index
        self._usb_width = usb_width
        self._usb_height = usb_height
        self._usb_fourcc = usb_fourcc
        self._usb_exposure = usb_exposure

        self._tof: Optional[ac.ArducamCamera] = None
        self._usb: Optional[cv2.VideoCapture] = None

        self._depth: Optional[np.ndarray] = None     # uint16 mm
        self._conf: Optional[np.ndarray] = None      # uint8
        self._depth_h: int = 0
        self._depth_w: int = 0

        self._px_per_mm: Optional[float] = None
        self._fx_cache: Optional[float] = fx_px
        self._fy_cache: Optional[float] = fy_px
        self._hfov_cal: Optional[float] = None
        self._vfov_cal: Optional[float] = None

        self._loop_task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

        # holds the last annotated frame if you want to export it
        self.last_annotated: Optional[np.ndarray] = None

    # -------------------- Lifecycle --------------------

    async def start(self) -> None:
        # Open TOF
        self._tof = ac.ArducamCamera()
        port_used = self._open_tof(self._tof, self.preferred_tof_port)
        if port_used < 0:
            raise RuntimeError("Failed to open TOF")

        if self._tof.start(ac.FrameType.DEPTH) != 0:
            self._tof.close()
            raise RuntimeError("Failed to start TOF stream")
        self._tof.setControl(ac.Control.RANGE, self.max_distance_mm)

        fr = self._tof.requestFrame(2000)
        if fr is not None and isinstance(fr, ac.DepthData):
            self._update_depth_buffers(fr.depth_data, fr.confidence_data)
            self._tof.releaseFrame(fr)

        # Open USB
        self._usb = cv2.VideoCapture(self._usb_index, cv2.CAP_V4L2)
        if not self._usb.isOpened():
            self._cleanup_tof()
            raise RuntimeError(f"Failed to open USB camera at index {self._usb_index}")
        if self._usb_width is not None:
            self._usb.set(cv2.CAP_PROP_FRAME_WIDTH, int(self._usb_width))
        if self._usb_height is not None:
            self._usb.set(cv2.CAP_PROP_FRAME_HEIGHT, int(self._usb_height))
        if self._usb_fourcc:
            fourcc = cv2.VideoWriter_fourcc(*self._usb_fourcc)
            self._usb.set(cv2.CAP_PROP_FOURCC, fourcc)
        if self._usb_exposure is not None:
            self._usb.set(cv2.CAP_PROP_EXPOSURE, float(self._usb_exposure))

        ok, frame0 = self._usb.read()
        if ok and frame0 is not None and (self._fx_cache is None or self._fy_cache is None):
            h0, w0 = frame0.shape[:2]
            fx, fy = compute_fx_fy_from_fov(w0, h0, self.hfov_deg, self.vfov_deg)
            self._fx_cache = self._fx_cache or fx
            self._fy_cache = self._fy_cache or fy

        self._stop_evt.clear()
        self._loop_task = asyncio.create_task(self._tof_loop())

    async def stop(self) -> None:
        self._stop_evt.set()
        if self._loop_task:
            try:
                await asyncio.wait_for(self._loop_task, timeout=1.5)
            except asyncio.TimeoutError:
                self._loop_task.cancel()
        self._cleanup_usb()
        self._cleanup_tof()

    # -------------------- Public API --------------------

    async def measure(
        self,
        n_frames: int = 5,
        timeout_s: float = 2.0,
        display: bool = False,
        window_name: str = "usb_sizing",
        bounding_box: Tuple[int, int, int, int] | None = None,  # <-- NEW
        bbox_inclusive: bool = True                               # <-- NEW
    ) -> Dict[str, Any]:
        
        if self._usb is None:
            raise RuntimeError("USB camera not started")
        frames: List[np.ndarray] = []
        loop = asyncio.get_running_loop()

        for _ in range(n_frames):
            ok, fr = await loop.run_in_executor(None, self._usb.read)
            if not ok or fr is None:
                continue
            frames.append(fr)

        if not frames:
            if display:
                self._show_text_frame("USB read failed", window_name)
            return {"ok": False, "reason": "usb_read_failed"}

        # Ensure scale at least once
        if self._px_per_mm is None:
            gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
            det = detect_checkerboard_scale_detailed(gray0, self.square_mm)
            if det is not None:
                self._px_per_mm = det["px_per_mm"]
                if (self._fx_cache is None or self._fy_cache is None) and self._depth is not None:
                    cal = self._calibrate_fov_from_checkerboard(det, gray0.shape)
                    if cal is not None:
                        fx_est, fy_est, hfov_est, vfov_est = cal
                        if self._fx_cache is None: self._fx_cache = fx_est
                        if self._fy_cache is None: self._fy_cache = fy_est
                        self._hfov_cal, self._vfov_cal = hfov_est, vfov_est
            if self._px_per_mm is None:
                self._px_per_mm = 1.7  # conservative fallback

        per_frame: List[Dict[str, Any]] = []
        last_draw = None

        for fr in frames:
            draw = fr.copy()
            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

            mask = segment_object_on_checkerboard(gray, self._px_per_mm, self.square_mm)
            contour = pick_main_contour(mask)

            if contour is None:
                if display:
                    _put_text(draw, "no object contour", (10, 30), 0.7, (0,0,255))
                    self._safe_show(draw, window_name)
                per_frame.append({"ok": False, "reason": "no_contour"})
                continue

            w_px, h_px, box, (cx, cy) = measure_contour_px(contour)
            # Ensure corners are int for emission and checking
            box_i = np.asarray(box, dtype=int)

            # ----- NEW: corner-in-bbox check -----
            corners_in_bbox = None
            oob_indices: List[int] = []
            if bounding_box is not None:
                corners_in_bbox, oob_indices = corners_within_bbox(box_i, bounding_box, inclusive=bbox_inclusive)

            size_w_mm_scale = w_px / self._px_per_mm
            size_h_mm_scale = h_px / self._px_per_mm
            a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)

            z_mm = self._sample_depth_at_usb_point((cx, cy), gray.shape)

            footprint = None
            if z_mm is not None and self._fx_cache is not None and self._fy_cache is not None:
                w_mm_fp, h_mm_fp = footprint_from_rect_pinhole(w_px, h_px, z_mm, self._fx_cache, self._fy_cache)
                a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                footprint = (a_fp, b_fp)

            # --------- Draw overlay ---------
            cv2.drawContours(draw, [contour], -1, (0, 255, 0), 2)
            cv2.polylines(draw, [box_i], True, (255, 0, 0), 2)  # minAreaRect box
            cv2.circle(draw, (int(cx), int(cy)), 4, (0, 255, 255), -1)

            # (Optional) draw the bounding box if provided
            if bounding_box is not None:
                x0, y0, x1, y1 = map(int, bounding_box)
                color = (0, 200, 0) if corners_in_bbox else (0, 0, 255)
                cv2.rectangle(draw, (x0, y0), (x1, y1), color, 2)

                # mark any out-of-bounds corners
                for idx in oob_indices:
                    px, py = int(box_i[idx, 0]), int(box_i[idx, 1])
                    cv2.circle(draw, (px, py), 5, (0, 0, 255), -1)

            depth_text = f"depth: {z_mm:.1f} mm" if z_mm is not None else "depth: n/a"
            if footprint:
                fp_text = f"footprint: {footprint[0]:.1f} × {footprint[1]:.1f} mm"
            else:
                fp_text = "footprint: n/a"

            info_line = f"scale: {a_scale:.1f} × {b_scale:.1f} mm | {depth_text} | {fp_text}"
            tx = max(10, int(cx) - 260)
            ty = max(20, int(cy) - 12)
            _put_text(draw, info_line, (tx, ty))

            status = f"px/mm={self._px_per_mm:.3f}"
            if self._fx_cache is not None and self._fy_cache is not None:
                status += f" | fx={self._fx_cache:.0f}, fy={self._fy_cache:.0f}"
            if self._hfov_cal is not None and self._vfov_cal is not None:
                status += f" | HFOV={self._hfov_cal:.1f}°, VFOV={self._vfov_cal:.1f}°"
            _put_text(draw, status, (10, 20))

            if display:
                self._safe_show(draw, window_name)

            self.last_annotated = draw
            last_draw = draw

            per_frame.append({
                "ok": True,
                "center_xy": (int(cx), int(cy)),
                "box": box_i.tolist(),                    # existing
                "box_corners": box_i.tolist(),            # explicit alias if you prefer
                "corners_in_bbox": corners_in_bbox,       # <-- NEW (None if no bbox provided)
                "oob_corner_indices": oob_indices,        # <-- NEW ([])
                "w_px": float(w_px),
                "h_px": float(h_px),
                "scale_mm": (float(a_scale), float(b_scale)),
                "depth_mm": float(z_mm) if z_mm is not None else None,
                "footprint_mm": (float(footprint[0]), float(footprint[1])) if footprint is not None else None
            })


    # -------------------- Internals --------------------

    def _show_text_frame(self, msg: str, window_name: str) -> None:
        blank = np.zeros((240, 320, 3), dtype=np.uint8)
        _put_text(blank, msg, (10, 120), 0.7)
        cv2.imshow(window_name, blank)
        cv2.waitKey(1)

    def _open_tof(self, cam: ac.ArducamCamera, preferred_port: Optional[int]) -> int:
        ports_to_try = [preferred_port] if preferred_port is not None else [0, 1]
        last_err = None
        for p in ports_to_try:
            ret = cam.open(ac.Connection.CSI, p)
            if ret == 0:
                return p
            last_err = ret
        print(f"[TOF] Failed to open on any CSI port. Last error: {last_err}")
        return -1

    def _update_depth_buffers(self, depth: np.ndarray, conf: np.ndarray) -> None:
        self._depth = depth.copy()
        self._conf = conf.copy()
        self._depth_h, self._depth_w = self._depth.shape[:2]

    async def _tof_loop(self) -> None:
        assert self._tof is not None
        try:
            while not self._stop_evt.is_set():
                fr = self._tof.requestFrame(200)
                if fr is not None and isinstance(fr, ac.DepthData):
                    self._update_depth_buffers(fr.depth_data, fr.confidence_data)
                    self._tof.releaseFrame(fr)
                await asyncio.sleep(0.001)
        except asyncio.CancelledError:
            pass

    def _cleanup_tof(self) -> None:
        if self._tof is not None:
            try:
                self._tof.stop()
            except Exception:
                pass
            try:
                self._tof.close()
            except Exception:
                pass
            self._tof = None

    def _cleanup_usb(self) -> None:
        if self._usb is not None:
            try:
                self._usb.release()
            except Exception:
                pass
            self._usb = None

    def _sample_depth_at_usb_point(self, u_xy: Tuple[int,int], usb_shape: Tuple[int,int], win_radius: int = 3) -> Optional[float]:
        if self._depth is None or self._conf is None:
            return None
        usb_h, usb_w = usb_shape
        x_u, y_u = u_xy
        sx = self._depth_w / float(usb_w)
        sy = self._depth_h / float(usb_h)
        x_d = int(round(x_u * sx))
        y_d = int(round(y_u * sy))

        x0 = max(0, x_d - win_radius)
        x1 = min(self._depth_w, x_d + win_radius + 1)
        y0 = max(0, y_d - win_radius)
        y1 = min(self._depth_h, y_d + win_radius + 1)
        if x0 >= x1 or y0 >= y1:
            return None

        depth_win = self._depth[y0:y1, x0:x1]
        conf_win  = self._conf[y0:y1, x0:x1]
        valid = (conf_win >= self.confidence_min) & (depth_win > 0) & (depth_win < self.max_distance_mm)
        if not np.any(valid):
            return None
        return float(np.mean(depth_win[valid]))

    def _calibrate_fov_from_checkerboard(self, det: dict, usb_shape: Tuple[int,int]) -> Optional[Tuple[float, float, float, float]]:
        if self._depth is None or self._conf is None:
            return None
        center_xy = det["center_xy"]
        z_mm = self._sample_depth_at_usb_point(center_xy, usb_shape=usb_shape)
        if z_mm is None or z_mm <= 0:
            return None

        px_per_mm_h = float(det["px_per_mm_h"])
        px_per_mm_v = float(det["px_per_mm_v"])
        spacing_h_px = px_per_mm_h * self.square_mm
        spacing_v_px = px_per_mm_v * self.square_mm

        fx = (spacing_h_px * z_mm) / self.square_mm
        fy = (spacing_v_px * z_mm) / self.square_mm

        H, W = usb_shape[:2]
        hfov = 2.0 * np.degrees(np.arctan((W / 2.0) / fx))
        vfov = 2.0 * np.degrees(np.arctan((H / 2.0) / fy))
        return float(fx), float(fy), float(hfov), float(vfov)
