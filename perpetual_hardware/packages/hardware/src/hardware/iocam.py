import asyncio
import os
import time
from typing import Optional, Tuple, List, Dict, Any

import cv2
import numpy as np
import ArducamDepthCamera as ac
import iocam_helpers as ih
import 


# ============================ Async Sizer Class ============================

from typing import Optional

class AFSCam:
    """
    Async wrapper for TOF + USB sizing with optional on-screen visualization.
    - await start()
    - await measure(n_frames=5, display=True, bounding_box=(x0,y0,x1,y1))
    - await stop()
    """

    def __init__(self, params: dict) -> None:
        # TOF parameters
        self.preferred_tof_port = params.get("preferred_tof_port", 2)   # 0 or 1 on Pi 5; None => try both
        self.max_distance_mm = params.get("max_distance_mm", 4000)      # 2000 or 4000

        # Calibration & geometry
        self.square_mm = params.get("square_mm", 7.5)
        self.fx_px = params.get("fx_px", 1150)
        self.fy_px = params.get("fy_px", 1150)
        self.confidence_min = params.get("confidence_min", 0)

        # Fallback / headless mode
        self._headless_out = params.get("headless_fallback_path", "./usb_sizing.jpg")

        # USB camera configuration
        self._usb_index = params.get("usb_index", 0)
        self._usb_fourcc = params.get("usb_fourcc", "MJPG")
        self._usb_exposure = params.get("usb_exposure", 100)

        # Hardware handles
        self._tof: Optional[ac.ArducamCamera] = None
        self._usb: Optional[cv2.VideoCapture] = None

        # Latest TOF frame buffers (updated in background loop)
        self._depth: Optional[np.ndarray] = None  # uint16 mm
        self._conf: Optional[np.ndarray] = None   # uint8
        self._depth_h: int = 0
        self._depth_w: int = 0

        # Calibration caches
        self._px_per_mm: Optional[float] = None
        self._fx_cache: Optional[float] = self.fx_px
        self._fy_cache: Optional[float] = self.fy_px
        self._hfov_cal: Optional[float] = None
        self._vfov_cal: Optional[float] = None

        # Async
        self._loop_task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

        # last annotated frame
        self.last_annotated: Optional[np.ndarray] = None

    # -------------------- Lifecycle --------------------

    async def start(self) -> None:
        # TOF
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

        # USB
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

        # Estimate fx/fy from FOV if needed
        ok, frame0 = self._usb.read()
        if ok and frame0 is not None and (self._fx_cache is None or self._fy_cache is None):
            h0, w0 = frame0.shape[:2]
            fx, fy = ih.compute_fx_fy_from_fov(w0, h0, self.hfov_deg, self.vfov_deg)
            self._fx_cache = self._fx_cache or fx
            self._fy_cache = self._fy_cache or fy

        # Start background TOF loop
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
        bounding_box: Optional[Tuple[int, int, int, int]] = None,  # (x0, y0, x1, y1)
        bbox_inclusive: bool = True,
        max_center_offset_px: float = 40.0  # existing param (kept)
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

        # Ensure checkerboard scale at least once
        if self._px_per_mm is None:
            gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
            det = ih.detect_checkerboard_scale_detailed(gray0, self.square_mm)
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
                self._px_per_mm = 1.7  # fallback

        per_frame: List[Dict[str, Any]] = []
        last_draw = None

        for fr in frames:
            draw = fr.copy()
            gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

            mask = ih.segment_object_on_checkerboard(gray, self._px_per_mm, self.square_mm)
            contour = ih.pick_main_contour(mask)

            if contour is None:
                per_frame.append({"ok": False, "reason": "no_contour"})
                continue

            w_px, h_px, box, (cx, cy) = ih.measure_contour_px(contour)
            box_i = np.asarray(box, dtype=int)

            # ----- Bounding box checks + center distance -----
            center_dist_px = None

            if bounding_box is not None:
                x0, y0, x1, y1 = map(int, bounding_box)

                # center-to-center distance in pixels
                bx_cx = (x0 + x1) / 2.0
                by_cy = (y0 + y1) / 2.0
                center_dist_px = float(np.hypot(cx - bx_cx, cy - by_cy))

            # ----- Scale and depth -----
            size_w_mm_scale = w_px / self._px_per_mm
            size_h_mm_scale = h_px / self._px_per_mm
            a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)

            z_mm = self._sample_depth_at_usb_point((cx, cy), gray.shape)
            footprint = None
            if z_mm is not None and self._fx_cache and self._fy_cache:
                w_mm_fp, h_mm_fp = ih.footprint_from_rect_pinhole(w_px, h_px, z_mm, self._fx_cache, self._fy_cache)
                a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                footprint = (a_fp, b_fp)

            per_frame.append({
                "ok": True,
                "center_xy": (int(cx), int(cy)),
                "center_dist_px": center_dist_px,          # <-- per-frame distance recorded
                "scale_mm": (float(a_scale), float(b_scale)),
                "depth_mm": float(z_mm) if z_mm is not None else None,
                "footprint_mm": (float(footprint[0]), float(footprint[1])) if footprint is not None else None
            })

        # ----- Aggregates -----
        ok_frames = [p for p in per_frame if p.get("ok")]
        agg: Dict[str, Any] = {}
        if ok_frames:
            def med(vals):
                vals = [v for v in vals if v is not None]
                return float(np.median(vals)) if vals else None

            agg["scale_major_mm_med"] = med([p["scale_mm"][0] for p in ok_frames])
            agg["scale_minor_mm_med"] = med([p["scale_mm"][1] for p in ok_frames])
            agg["depth_mm_med"]       = med([p["depth_mm"] for p in ok_frames])

            # NEW: center distance median (pixels), only when bbox was provided
            if bounding_box is not None:
                dists = [p["center_dist_px"] for p in ok_frames if p.get("center_dist_px") is not None]
                agg["center_dist_px_med"] = med(dists)

        return {
            "ok": bool(ok_frames),
            # "per_frame": per_frame,
            "aggregate": agg
        }

    # -------------------- Internals --------------------

    def _open_tof(self, cam: ac.ArducamCamera, preferred_port: Optional[int]) -> int:
        ports_to_try = [preferred_port] if preferred_port is not None else [0, 1, 2]
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

    def _sample_depth_at_usb_point(self, u_xy: Tuple[int, int], usb_shape: Tuple[int, int], win_radius: int = 3) -> Optional[float]:
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

    def _calibrate_fov_from_checkerboard(self, det: dict, usb_shape: Tuple[int, int]) -> Optional[Tuple[float, float, float, float]]:
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