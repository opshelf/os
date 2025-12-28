import asyncio
import time
import re
import subprocess
from typing import Optional, Tuple, List, Dict, Any, TYPE_CHECKING

import cv2
import numpy as np

try:
    import ArducamDepthCamera as ac
except ImportError:
    ac = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from ArducamDepthCamera import ArducamCamera  # type: ignore[misc]
else:
    ArducamCamera = Any  # type: ignore

from hardware import iocam_helpers as ih
from hardware.exceptions import (
    CameraError, CameraConnectionError, CameraConfigError,
    ImageError, ImageProcessingError, TOFError, TOFConfigError, TOFDataError
)

# ============================ Linux /dev/ helper ============================


def _find_innomaker_device() -> int:
    """Return the /dev/video index for the Innomaker camera."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Innomaker-U20CAM-1080p-S1" in line:
                # The next line usually contains /dev/videoX
                if i + 1 < len(lines):
                    match = re.search(r"/dev/video(\d+)", lines[i + 1])
                    if match:
                        return int(match.group(1))
        raise RuntimeError("Innomaker U20CAM not found in v4l2-ctl output.")
    except Exception as e:
        raise RuntimeError(f"Failed to detect Innomaker camera: {e}")

# ============================ Async Sizer Class ============================

Z_MAX = 620

class AFSCam:
    """
    Async wrapper for TOF + USB sizing with optional on-screen visualization.
    - await start()
    - await measure(n_frames=5, display=True, bounding_box=(x0,y0,x1,y1))
    - await stop()
    """

    def __init__(self, usb_params, tof_params=None) -> None:
        self._tof_params = tof_params

        if tof_params is not None:
            self.preferred_tof_port = getattr(tof_params, "csi_port", None)
            self.max_distance_mm = tof_params.max_distance_mm
            self.confidence_min = tof_params.confidence_min
        else:
            self.preferred_tof_port = None
            self.max_distance_mm = 620
            self.confidence_min = 0

        self.square_mm = usb_params.square_mm
        self.fx_px = usb_params.fx_px
        self.fy_px = usb_params.fy_px
        self.px_per_mm = usb_params.px_per_mm

        # USB camera configuration
        self._usb_index = usb_params.usb_index
        self._usb_fourcc = usb_params.usb_fourcc
        self._usb_exposure = usb_params.usb_exposure

        # Hardware handles
        self._tof: Optional[ArducamCamera] = None
        self._usb: Optional[cv2.VideoCapture] = None

        # Latest TOF frame buffers (updated in background loop)
        self._depth: Optional[np.ndarray] = None  # uint16 mm
        self._conf: Optional[np.ndarray] = None   # uint8
        self._depth_h: int = 0
        self._depth_w: int = 0

        # Calibration caches
        self._px_per_mm: Optional[float] = self.px_per_mm
        self._px_per_mm_xy: Optional[Tuple[float, float]] = (
            (float(self.px_per_mm), float(self.px_per_mm))
            if self.px_per_mm is not None else None
        )
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

    def _init_tof(self) -> None:
        """Initialize TOF camera."""
        if ac is None:
            raise TOFError(
                "ArducamDepthCamera is not installed. Install the hardware extras to "
                "enable TOF camera support."
            )
        try:
            # print("initializing")
            self._tof = ac.ArducamCamera()
            port_used = self._open_tof(self._tof, self.preferred_tof_port)
            if port_used < 0:
                # print("bad port")
                raise TOFError(f"Failed to open TOF on port {self.preferred_tof_port}")
                
            if self._tof.start(ac.FrameType.DEPTH) != 0:
                self._tof.close()
                print("bad stream")
                raise TOFError("Failed to start TOF stream")
                
            try:
                self._tof.setControl(ac.Control.RANGE, self.max_distance_mm)
            except Exception as e:
                # print("bad range")
                raise TOFConfigError(
                    f"Failed to set TOF range to {self.max_distance_mm}mm: {str(e)}"
                ) from e
            
            # Get initial frame to verify operation
            fr = self._tof.requestFrame(2000)
            if fr is None:
                raise TOFDataError("Failed to get initial TOF frame")

            if not isinstance(fr, ac.DepthData):
                self._tof.releaseFrame(fr)
                raise TOFDataError("Invalid TOF frame type")
                
            try:
                self._update_depth_buffers(fr.depth_data, fr.confidence_data)
            finally:
                self._tof.releaseFrame(fr)
                
        except Exception as e:
            self._cleanup_tof()
            if not isinstance(e, TOFError):
                raise TOFError(f"TOF initialization failed: {str(e)}") from e
            raise

    def _init_usb(self, use_auto_exposure: bool = False) -> None:
        """Initialize USB camera (auto-detect Innomaker U20CAM)."""
        try:
            # Auto-detect Innomaker camera
            usb_index = _find_innomaker_device()
            self._usb_index = usb_index
            print(f"[USB] Found Innomaker U20CAM at /dev/video{usb_index}")

            # Open using V4L2 backend
            self._usb = cv2.VideoCapture(usb_index, cv2.CAP_V4L2)
            if not self._usb.isOpened():
                raise CameraConnectionError(f"Failed to open Innomaker at index {usb_index}")

            # Configure FOURCC
            if self._usb_fourcc:
                fourcc = cv2.VideoWriter_fourcc(*self._usb_fourcc)
                if not self._usb.set(cv2.CAP_PROP_FOURCC, fourcc):
                    raise CameraConfigError(f"Failed to set FOURCC to {self._usb_fourcc}")

            # Configure exposure
            if use_auto_exposure is True:
                self._usb.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
            elif self._usb_exposure is not None:
                if not self._usb.set(cv2.CAP_PROP_EXPOSURE, float(self._usb_exposure)):
                    raise CameraConfigError(f"Failed to set exposure {self._usb_exposure}")

            # Get initial frame and compute FOV parameters
            ok, frame0 = self._usb.read()
            if not ok or frame0 is None:
                raise CameraError("Failed to capture initial frame")

        except Exception as e:
            self._cleanup_usb()
            if not isinstance(e, (CameraConnectionError, CameraConfigError)):
                raise CameraError(f"USB camera initialization failed: {e}") from e
            raise
                
    async def start(self, enable_tof: bool = True) -> None:
        """
        Initialize and start cameras.

        Args:
            enable_tof: If True, initialize TOF camera for depth measurement.
                       If False, only USB camera is initialized (monitoring mode with auto-exposure).

        Raises:
            TOFError: If TOF camera initialization or configuration fails
            CameraConnectionError: If USB camera connection fails
            CameraConfigError: If camera configuration fails
            CameraError: If general camera initialization fails
        """
        try:
            use_auto_exposure = enable_tof is False
            self._init_usb(use_auto_exposure=use_auto_exposure)

            if enable_tof is True and self._tof_params is not None:
                self._init_tof()
                self._stop_evt.clear()
                self._loop_task = asyncio.create_task(self._tof_loop())

        except Exception as e:
            self._cleanup()
            if not isinstance(e, (TOFError, CameraError)):
                raise CameraError(f"Camera initialization failed: {str(e)}") from e
            raise
            
    def _cleanup_tof(self) -> None:
        """Clean up TOF camera resources."""
        if self._tof is not None:
            try:
                self._tof.close()
            except Exception:
                pass  # Best effort cleanup
            self._tof = None
            
    def _cleanup_usb(self) -> None:
        """Clean up USB camera resources."""
        if self._usb is not None:
            try:
                self._usb.release()
            except Exception:
                pass  # Best effort cleanup
            self._usb = None
            
    def _cleanup(self) -> None:
        """Clean up all camera resources."""
        self._cleanup_tof()
        self._cleanup_usb()

    async def stop(self) -> None:
        """
        Stop camera operations and clean up resources.
        
        Raises:
            CameraError: If cleanup fails
        """
        try:
            # Signal loop to stop
            self._stop_evt.set()
            
            # Wait for background task to finish
            if self._loop_task:
                try:
                    await asyncio.wait_for(self._loop_task, timeout=1.5)
                except asyncio.TimeoutError:
                    self._loop_task.cancel()
                    await asyncio.sleep(0.1)  # Brief pause to allow cancellation
                except Exception as e:
                    raise CameraError(
                        f"Error stopping background task: {str(e)}"
                    ) from e
                finally:
                    self._loop_task = None
                    
            # Cleanup resources
            try:
                self._cleanup_usb()
                self._cleanup_tof()
            except Exception as e:
                raise CameraError(f"Error during cleanup: {str(e)}") from e
                
        except Exception as e:
            if not isinstance(e, CameraError):
                raise CameraError(f"Failed to stop cameras: {str(e)}") from e
            raise

    # -------------------- Public API --------------------

    def capture_raw(self) -> Optional[np.ndarray]:
        """Capture a single raw frame from USB camera without any processing."""
        if self._usb is None:
            return None

        ok, frame = self._usb.read()
        if ok is True and frame is not None:
            return frame

        return None

    def measure_sync(
        self,
        n_frames: int = 30,
        timeout_s: float = 2.0,
        display: bool = False,
        window_name: str = "usb_sizing",
        bounding_box: Optional[Tuple[int, int, int, int]] = (316,266,316,266),  # (x0, y0, x1, y1)
        bbox_inclusive: bool = True,
        max_center_offset_px: float = 40.0
    ) -> Dict[str, Any]:
        """
        Synchronous variant that draws the distance between the given bounding box center
        and the detected contour center on the USB frame, and also overlays the *depth
        sampling location* (crosshair + sampling window) projected on the USB frame.
        """
        if self._usb is None:
            raise CameraConnectionError("USB camera not started")

        if n_frames < 1:
            raise ValueError("n_frames must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if bounding_box is not None:
            if len(bounding_box) != 4:
                raise ValueError("bounding_box must have 4 elements")
            if any(x < 0 for x in bounding_box):
                raise ValueError("bounding_box values must be non-negative")
        if max_center_offset_px <= 0:
            raise ValueError("max_center_offset_px must be positive")

        frames: List[np.ndarray] = []
        capture_start = time.monotonic()
        frames_captured = 0

        try:
            while frames_captured < n_frames:
                if time.monotonic() - capture_start > timeout_s:
                    raise ImageError(f"Frame capture timeout after {frames_captured} frames")
                ok, frame = self._usb.read()
                if not ok:
                    raise ImageError("Frame capture failed")
                if frame is None:
                    raise ImageError("Received null frame")
                frames.append(frame)
                frames_captured += 1

            if not frames:
                if display:
                    show_fn = getattr(self, "_show_text_frame", None)
                    if callable(show_fn):
                        show_fn("USB read failed", window_name)
                return {"ok": False, "reason": "usb_read_failed"}

            per_frame: List[Dict[str, Any]] = []
            last_draw: Optional[np.ndarray] = None

            for fr in frames:
                try:
                    draw = fr.copy()
                    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

                    # --------- ROI box + center distance scaffolding ----------
                    bbox_center_xy: Optional[Tuple[float, float]] = None
                    bbox_center_int: Optional[Tuple[int, int]] = None
                    if bounding_box is not None:
                        try:
                            x0, y0, x1, y1 = map(int, bounding_box)
                            if bbox_inclusive:
                                bx_cx = (x0 + x1) / 2.0
                                by_cy = (y0 + y1) / 2.0
                            else:
                                bx_cx = (x0 + x1 - 1) / 2.0
                                by_cy = (y0 + y1 - 1) / 2.0
                            bbox_center_xy = (float(bx_cx), float(by_cy))
                            bbox_center_int = (int(round(bx_cx)), int(round(by_cy)))
                            cv2.rectangle(draw, (x0, y0), (x1, y1), (0, 255, 0), 2)
                        except Exception as e:
                            raise ImageProcessingError(
                                f"Bounding box calculation failed: {str(e)}"
                            ) from e

                    # --------- Contour detection + visualization ----------
                    contour = ih.lid_mask_tuned(fr)
                    if contour is None:
                        per_frame.append({"ok": False, "reason": "no_contour"})
                        last_draw = draw
                        continue

                    w_px, h_px, box, (cx, cy) = ih.measure_contour_px(ih.sanitize_contour(contour))

                    box_i = np.asarray(box, dtype=int)
                    cv2.drawContours(draw, [box_i], 0, (0, 0, 255), 2)

                    center_dist_px = None
                    if bbox_center_xy is not None:
                        bx_cx, by_cy = bbox_center_xy
                        center_dist_px = float(np.hypot(cx - bx_cx, cy - by_cy))
                        pt_bbox = (int(round(bx_cx)), int(round(by_cy)))
                        pt_contour = (int(round(cx)), int(round(cy)))
                        cv2.circle(draw, pt_bbox, 4, (0, 255, 0), -1)
                        cv2.circle(draw, pt_contour, 4, (0, 0, 255), -1)
                        cv2.line(draw, pt_bbox, pt_contour, (255, 255, 0), 2)
                        label_x = int(round((pt_bbox[0] + pt_contour[0]) / 2.0))
                        label_y = int(round((pt_bbox[1] + pt_contour[1]) / 2.0)) - 10
                        label_x = max(0, min(draw.shape[1] - 1, label_x))
                        label_y = max(15, min(draw.shape[0] - 1, label_y))
                        cv2.putText(
                            draw, f"{center_dist_px:.1f}px", (label_x, label_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA
                        )
                    else:
                        cv2.circle(draw, (int(round(cx)), int(round(cy))), 4, (0, 0, 255), -1)

                    # --------- Scale-only size in mm ----------
                    try:
                        size_w_mm_scale = w_px / self.px_per_mm
                        size_h_mm_scale = h_px / self.px_per_mm
                        a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Scale calculation failed: {str(e)}"
                        ) from e

                    # --------- Depth sampling + overlay + footprint ----------
                    try:
                        # Depth sampling (keep your original averaging + bias)
                        values = [self._sample_depth_at_usb_point_shift_depth_dependent((cx, cy), gray.shape) for _ in range(10)]
                        # print(values)
                        z_mm = (sum(values) / len(values) if values else None)
                        if z_mm is not None:
                            z_mm = z_mm-55  # keep your bias if desired
                            # print(z_mm)

                        # ---- Draw depth sampling location (projected to USB frame) ----
                        H_u, W_u = gray.shape[:2]
                        H_d = getattr(self, "_depth_h", None)
                        W_d = getattr(self, "_depth_w", None)
                        if (H_d is None or W_d is None) and getattr(self, "_depth", None) is not None and hasattr(self._depth, "shape"):
                            H_d, W_d = self._depth.shape[:2]

                        if H_d and W_d:
                            sx = W_d / float(W_u)
                            sy = H_d / float(H_u)

                            # Optional user-maintained shift (in *depth* pixels)
                            shift_dx, shift_dy = (0.0, 0.0)
                            if hasattr(self, "_depth_pixel_shift") and isinstance(self._depth_pixel_shift, (tuple, list)) and len(self._depth_pixel_shift) == 2:
                                try:
                                    shift_dx = float(self._depth_pixel_shift[0])
                                    shift_dy = float(self._depth_pixel_shift[1])
                                except Exception:
                                    shift_dx, shift_dy = (0.0, 0.0)

                            # Depth coords actually sampled
                            x_d = int(round(cx * sx + shift_dx))
                            y_d = int(round(cy * sy + shift_dy))
                            x_d = max(0, min(W_d - 1, x_d))
                            y_d = max(0, min(H_d - 1, y_d))

                            # Sampling window radius (depth pixels)
                            win_r_d = 3
                            if hasattr(self, "_depth_win_radius"):
                                try:
                                    win_r_d = max(1, int(self._depth_win_radius))
                                except Exception:
                                    pass

                            # Back-project window extents into USB pixels for drawing
                            r_u_x = max(1, int(round(win_r_d / sx)))
                            r_u_y = max(1, int(round(win_r_d / sy)))

                            # Back-projected USB center
                            x_u_vis = int(round(x_d / sx))
                            y_u_vis = int(round(y_d / sy))
                            x_u_vis = max(0, min(W_u - 1, x_u_vis))
                            y_u_vis = max(0, min(H_u - 1, y_u_vis))

                            # Crosshair + window (cyan)
                            cv2.drawMarker(draw, (x_u_vis, y_u_vis), (255, 255, 0),
                                        markerType=cv2.MARKER_CROSS, markerSize=10, thickness=2)
                            x0s = max(0, x_u_vis - r_u_x)
                            y0s = max(0, y_u_vis - r_u_y)
                            x1s = min(W_u - 1, x_u_vis + r_u_x)
                            y1s = min(H_u - 1, y_u_vis + r_u_y)
                            cv2.rectangle(draw, (x0s, y0s), (x1s, y1s), (255, 255, 0), 1)

                            if z_mm is not None:
                                lbl = f"{z_mm:.1f} mm"
                                ty = max(15, y0s - 6)
                                cv2.putText(draw, lbl, (x0s, ty),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA)

                        # Footprint via pinhole if fx/fy present
                        footprint = None
                        if z_mm is not None and self._fx_cache and self._fy_cache:
                            w_mm_fp, h_mm_fp = ih.footprint_from_rect_pinhole(
                                w_px, h_px, z_mm, self._fx_cache, self._fy_cache
                            )
                            a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                            footprint = (a_fp, b_fp)
                            # Optionally use span to mm:
                            # span_mm = (span * self._fx_cache) / z_mm

                    except Exception as e:
                        raise ImageProcessingError(
                            f"Depth/footprint calculation failed: {str(e)}"
                        ) from e

                    # --------- On-frame text block ----------
                    dim_lines = [
                        f"Scale: {size_w_mm_scale:.1f}mm x {size_h_mm_scale:.1f}mm"
                    ]
                    if footprint is not None and z_mm is not None:
                        dim_lines.append(
                            f"Footprint: {footprint[0]:.1f}mm x {footprint[1]:.1f}mm @ depth={z_mm:.1f}mm"
                        )

                    text_y = draw.shape[0] - 10
                    for line_text in reversed(dim_lines):
                        cv2.putText(
                            draw, line_text, (10, text_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA
                        )
                        text_y -= 20

                    per_frame.append({
                        "ok": True,
                        "center_xy": (int(cx), int(cy)),
                        "bbox_center_xy": bbox_center_int,
                        "center_dist_px": center_dist_px,
                        "scale_mm": (float(a_scale), float(b_scale)),
                        "depth_mm": float(z_mm) if z_mm is not None else None,
                        "footprint_mm": (float(footprint[0]), float(footprint[1]))
                            if footprint is not None else None
                    })

                    last_draw = draw

                except Exception as e:
                    if not isinstance(e, ImageProcessingError):
                        raise ImageProcessingError(
                            f"Frame processing failed: {str(e)}"
                        ) from e
                    raise

            # --------- Aggregate ----------
            try:
                ok_frames = [p for p in per_frame if p["ok"]]
                agg: Dict[str, Any] = {}

                if ok_frames:
                    def med(vals):
                        vals = [v for v in vals if v is not None]
                        return float(np.median(vals)) if vals else None

                    agg["scale_major_mm_med"] = med([p["scale_mm"][0] for p in ok_frames])
                    agg["scale_minor_mm_med"] = med([p["scale_mm"][1] for p in ok_frames])
                    agg["depth_mm_med"] = med([p["depth_mm"] for p in ok_frames])

                    footprints = [p["footprint_mm"] for p in ok_frames if p["footprint_mm"] is not None]
                    if footprints:
                        agg["footprint_major_mm_med"] = med([fp[0] for fp in footprints])
                        agg["footprint_minor_mm_med"] = med([fp[1] for fp in footprints])

                    if bounding_box is not None:
                        dists = [p["center_dist_px"] for p in ok_frames if p["center_dist_px"] is not None]
                        agg["center_dist_px_med"] = med(dists)

                result: Dict[str, Any] = {
                    "ok": bool(ok_frames),
                    "aggregate": agg,
                    "per_frame": per_frame
                }

                if last_draw is not None:
                    self.last_annotated = last_draw
                    result["annotated_frame"] = last_draw
                    if display:
                        cv2.imshow(window_name, last_draw)
                        cv2.waitKey(1)
                elif display:
                    show_fn = getattr(self, "_show_text_frame", None)
                    if callable(show_fn):
                        show_fn("No contour detected", window_name)

                return result

            except Exception as e:
                raise ImageProcessingError(
                    f"Aggregate calculation failed: {str(e)}"
                ) from e

        except Exception as e:
            if not isinstance(e, (ImageError, ImageProcessingError)):
                raise ImageProcessingError(f"Measurement failed: {str(e)}") from e
            raise


    async def measure(
        self,
        n_frames: int = 15,
        timeout_s: float = 2.0,
        display: bool = False,
        window_name: str = "usb_sizing",
        bounding_box: Optional[Tuple[int, int, int, int]] = (316,266,316,266),  # (x0, y0, x1, y1)
        bbox_inclusive: bool = True,
        max_center_offset_px: float = 40.0
    ) -> Dict[str, Any]:
        """
        Synchronous variant that draws the distance between the given bounding box center
        and the detected contour center on the USB frame, and also overlays the *depth
        sampling location* (crosshair + sampling window) projected on the USB frame.
        """
        if self._usb is None:
            raise CameraConnectionError("USB camera not started")

        if n_frames < 1:
            raise ValueError("n_frames must be positive")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if bounding_box is not None:
            if len(bounding_box) != 4:
                raise ValueError("bounding_box must have 4 elements")
            if any(x < 0 for x in bounding_box):
                raise ValueError("bounding_box values must be non-negative")
        if max_center_offset_px <= 0:
            raise ValueError("max_center_offset_px must be positive")

        frames: List[np.ndarray] = []
        try:
            time_fn = asyncio.get_running_loop().time
        except RuntimeError:
            time_fn = time.monotonic

        capture_start = time_fn()
        frames_captured = 0

        try:
            while frames_captured < n_frames:
                if time_fn() - capture_start > timeout_s:
                    raise ImageError(f"Frame capture timeout after {frames_captured} frames")
                ok, frame = self._usb.read()
                if not ok:
                    raise ImageError("Frame capture failed")
                if frame is None:
                    raise ImageError("Received null frame")
                frames.append(frame)
                frames_captured += 1

            if not frames:
                if display:
                    show_fn = getattr(self, "_show_text_frame", None)
                    if callable(show_fn):
                        show_fn("USB read failed", window_name)
                return {"ok": False, "reason": "usb_read_failed"}

            # --------- One-time px/mm + optional FOV caches ----------
            if self._px_per_mm is None:
                gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
                det = ih.detect_checkerboard_scale_detailed(gray0, self.square_mm)
                if det is None:
                    raise ImageProcessingError("Failed to detect checkerboard scale")

                try:
                    self._px_per_mm = float(det["px_per_mm"])
                except (KeyError, TypeError, ValueError):
                    self._px_per_mm = None

                px_h = det.get("px_per_mm_h") if isinstance(det, dict) else None
                px_v = det.get("px_per_mm_v") if isinstance(det, dict) else None
                if self._px_per_mm is not None:
                    self._px_per_mm_xy = (
                        float(px_h) if px_h is not None else float(self._px_per_mm),
                        float(px_v) if px_v is not None else float(self._px_per_mm),
                    )

                if self._px_per_mm is None:
                    self._px_per_mm = 1.7  # fallback value
                if self._px_per_mm_xy is None and self._px_per_mm is not None:
                    self._px_per_mm_xy = (float(self._px_per_mm), float(self._px_per_mm))

            per_frame: List[Dict[str, Any]] = []
            last_draw: Optional[np.ndarray] = None

            for fr in frames:
                try:
                    draw = fr.copy()
                    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

                    # --------- ROI box + center distance scaffolding ----------
                    bbox_center_xy: Optional[Tuple[float, float]] = None
                    bbox_center_int: Optional[Tuple[int, int]] = None
                    if bounding_box is not None:
                        try:
                            x0, y0, x1, y1 = map(int, bounding_box)
                            if bbox_inclusive:
                                bx_cx = (x0 + x1) / 2.0
                                by_cy = (y0 + y1) / 2.0
                            else:
                                bx_cx = (x0 + x1 - 1) / 2.0
                                by_cy = (y0 + y1 - 1) / 2.0
                            bbox_center_xy = (float(bx_cx), float(by_cy))
                            bbox_center_int = (int(round(bx_cx)), int(round(by_cy)))
                            cv2.rectangle(draw, (x0, y0), (x1, y1), (0, 255, 0), 2)
                        except Exception as e:
                            raise ImageProcessingError(
                                f"Bounding box calculation failed: {str(e)}"
                            ) from e

                    # --------- Contour detection + visualization ----------
                    contour = ih.lid_mask_tuned(fr)
                    if contour is None:
                        per_frame.append({"ok": False, "reason": "no_contour"})
                        last_draw = draw
                        continue

                    w_px, h_px, box, (cx, cy) = ih.measure_contour_px(ih.sanitize_contour(contour))
                    box_i = np.asarray(box, dtype=int)
                    cv2.drawContours(draw, [box_i], 0, (0, 0, 255), 2)

                    center_dist_px = None
                    if bbox_center_xy is not None:
                        bx_cx, by_cy = bbox_center_xy
                        center_dist_px = float(np.hypot(cx - bx_cx, cy - by_cy))
                        pt_bbox = (int(round(bx_cx)), int(round(by_cy)))
                        pt_contour = (int(round(cx)), int(round(cy)))
                        cv2.circle(draw, pt_bbox, 4, (0, 255, 0), -1)
                        cv2.circle(draw, pt_contour, 4, (0, 0, 255), -1)
                        cv2.line(draw, pt_bbox, pt_contour, (255, 255, 0), 2)
                        label_x = int(round((pt_bbox[0] + pt_contour[0]) / 2.0))
                        label_y = int(round((pt_bbox[1] + pt_contour[1]) / 2.0)) - 10
                        label_x = max(0, min(draw.shape[1] - 1, label_x))
                        label_y = max(15, min(draw.shape[0] - 1, label_y))
                        cv2.putText(
                            draw, f"{center_dist_px:.1f}px", (label_x, label_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA
                        )
                    else:
                        cv2.circle(draw, (int(round(cx)), int(round(cy))), 4, (0, 0, 255), -1)

                    # --------- Scale-only size in mm ----------
                    try:
                        size_w_mm_scale = w_px / self.px_per_mm
                        size_h_mm_scale = h_px / self.px_per_mm
                        a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Scale calculation failed: {str(e)}"
                        ) from e

                    # --------- Depth sampling + overlay + footprint ----------
                    try:
                        values = [self._sample_depth_at_usb_point_shift_depth_dependent((cx, cy), gray.shape) for _ in range(10)]
                        # print(values)
                        z_mm = (sum(values) / len(values) if values else None)
                        if z_mm is not None:
                            z_mm = z_mm-55  # keep your bias if desired
                            # print(z_mm)

                        # ---- Draw depth sampling location (projected to USB frame) ----
                        H_u, W_u = gray.shape[:2]
                        H_d = getattr(self, "_depth_h", None)
                        W_d = getattr(self, "_depth_w", None)
                        if (H_d is None or W_d is None) and getattr(self, "_depth", None) is not None and hasattr(self._depth, "shape"):
                            H_d, W_d = self._depth.shape[:2]

                        if H_d and W_d:
                            sx = W_d / float(W_u)
                            sy = H_d / float(H_u)

                            # Optional user-maintained shift (in *depth* pixels)
                            shift_dx, shift_dy = (0.0, 0.0)
                            if hasattr(self, "_depth_pixel_shift") and isinstance(self._depth_pixel_shift, (tuple, list)) and len(self._depth_pixel_shift) == 2:
                                try:
                                    shift_dx = float(self._depth_pixel_shift[0])
                                    shift_dy = float(self._depth_pixel_shift[1])
                                except Exception:
                                    shift_dx, shift_dy = (0.0, 0.0)

                            # Depth coords actually sampled
                            x_d = int(round(cx * sx + shift_dx))
                            y_d = int(round(cy * sy + shift_dy))
                            x_d = max(0, min(W_d - 1, x_d))
                            y_d = max(0, min(H_d - 1, y_d))

                            # Sampling window radius (depth pixels)
                            win_r_d = 3
                            if hasattr(self, "_depth_win_radius"):
                                try:
                                    win_r_d = max(1, int(self._depth_win_radius))
                                except Exception:
                                    pass

                            # Back-project window extents into USB pixels for drawing
                            r_u_x = max(1, int(round(win_r_d / sx)))
                            r_u_y = max(1, int(round(win_r_d / sy)))

                            # Back-projected USB center
                            x_u_vis = int(round(x_d / sx))
                            y_u_vis = int(round(y_d / sy))
                            x_u_vis = max(0, min(W_u - 1, x_u_vis))
                            y_u_vis = max(0, min(H_u - 1, y_u_vis))

                            # Crosshair + window (cyan)
                            cv2.drawMarker(draw, (x_u_vis, y_u_vis), (255, 255, 0),
                                        markerType=cv2.MARKER_CROSS, markerSize=10, thickness=2)
                            x0s = max(0, x_u_vis - r_u_x)
                            y0s = max(0, y_u_vis - r_u_y)
                            x1s = min(W_u - 1, x_u_vis + r_u_x)
                            y1s = min(H_u - 1, y_u_vis + r_u_y)
                            cv2.rectangle(draw, (x0s, y0s), (x1s, y1s), (255, 255, 0), 1)

                            if z_mm is not None:
                                lbl = f"{z_mm:.1f} mm"
                                ty = max(15, y0s - 6)
                                cv2.putText(draw, lbl, (x0s, ty),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1, cv2.LINE_AA)

                        # Footprint via pinhole if fx/fy present
                        footprint = None
                        if z_mm is not None and self._fx_cache and self._fy_cache:
                            w_mm_fp, h_mm_fp = ih.footprint_from_rect_pinhole(
                                w_px, h_px, z_mm, self._fx_cache, self._fy_cache
                            )
                            a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                            footprint = (a_fp, b_fp)
                            # Optionally use span to mm:
                            # span_mm = (span * self._fx_cache) / z_mm

                    except Exception as e:
                        raise ImageProcessingError(
                            f"Depth/footprint calculation failed: {str(e)}"
                        ) from e

                    # --------- On-frame text block ----------
                    dim_lines = [
                        f"Scale: {size_w_mm_scale:.1f}mm x {size_h_mm_scale:.1f}mm"
                    ]
                    if footprint is not None and z_mm is not None:
                        dim_lines.append(
                            f"Footprint: {footprint[0]:.1f}mm x {footprint[1]:.1f}mm @ z={620-z_mm:.1f}mm"
                        )

                    text_y = draw.shape[0] - 10
                    for line_text in reversed(dim_lines):
                        cv2.putText(
                            draw, line_text, (10, text_y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv2.LINE_AA
                        )
                        text_y -= 20

                    per_frame.append({
                        "ok": True,
                        "center_xy": (int(cx), int(cy)),
                        "bbox_center_xy": bbox_center_int,
                        "center_dist_px": center_dist_px,
                        "scale_mm": (float(a_scale), float(b_scale)),
                        "depth_mm": float(z_mm) if z_mm is not None else None,
                        "footprint_mm": (float(footprint[0]), float(footprint[1]))
                            if footprint is not None else None
                    })

                    last_draw = draw

                except Exception as e:
                    if not isinstance(e, ImageProcessingError):
                        raise ImageProcessingError(
                            f"Frame processing failed: {str(e)}"
                        ) from e
                    raise

            # --------- Aggregate ----------
            try:
                ok_frames = [p for p in per_frame if p["ok"]]
                agg: Dict[str, Any] = {}

                if ok_frames:
                    def med(vals):
                        vals = [v for v in vals if v is not None]
                        return float(np.median(vals)) if vals else None

                    agg["scale_major_mm_med"] = med([p["scale_mm"][0] for p in ok_frames])
                    agg["scale_minor_mm_med"] = med([p["scale_mm"][1] for p in ok_frames])
                    agg["depth_mm_med"] = med([p["depth_mm"] for p in ok_frames])

                    footprints = [p["footprint_mm"] for p in ok_frames if p["footprint_mm"] is not None]
                    if footprints:
                        agg["footprint_major_mm_med"] = med([fp[0] for fp in footprints])
                        agg["footprint_minor_mm_med"] = med([fp[1] for fp in footprints])

                    if bounding_box is not None:
                        dists = [p["center_dist_px"] for p in ok_frames if p["center_dist_px"] is not None]
                        agg["center_dist_px_med"] = med(dists)

                result: Dict[str, Any] = {
                    "ok": bool(ok_frames),
                    "aggregate": agg,
                    "per_frame": per_frame
                }

                if last_draw is not None:
                    self.last_annotated = last_draw
                    result["annotated_frame"] = last_draw
                    if display:
                        cv2.imshow(window_name, last_draw)
                        cv2.waitKey(1)
                elif display:
                    show_fn = getattr(self, "_show_text_frame", None)
                    if callable(show_fn):
                        show_fn("No contour detected", window_name)

                return result

            except Exception as e:
                raise ImageProcessingError(
                    f"Aggregate calculation failed: {str(e)}"
                ) from e

        except Exception as e:
            if not isinstance(e, (ImageError, ImageProcessingError)):
                raise ImageProcessingError(f"Measurement failed: {str(e)}") from e
            raise
    

    # -------------------- Internals --------------------

    def _open_tof(self, cam: ArducamCamera, preferred_port: Optional[int]) -> int:
        ports_to_try = [preferred_port] if preferred_port is not None else [0, 1, 2]
        last_err = None
        for p in ports_to_try:
            ret = cam.open(ac.Connection.CSI, p)
            if ret == 0:
                # print(p)
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

    def _sample_depth_at_usb_point_shift_depth_dependent(
        self,
        u_xy: Tuple[int, int],
        usb_shape: Tuple[int, int],
        *,
        win_radius: int = 5,
        # Stereo geometry
        fy_depth_px: float = 214.0,  # focal length (pixels) of the DEPTH camera
        baseline_mm: float = 50.0,   # camera center-to-center baseline in mm
        disparity_sign: int = +1,    # +1 if depth cam is ABOVE of USB cam; -1 if BELOW
        cy_offset_px: float = 10.0,   # optional constant x-offset in DEPTH pixels (principal point / small misalign)
        max_iters: int = 2           # do 1 or 2 iterations; 2 is usually enough
    ) -> Optional[float]:
        """
        Map a USB pixel into the depth map and return the mean valid depth (mm),
        using a depth-dependent vertical pixel shift (stereo disparity).
        """
        if self._depth is None or self._conf is None:
            return None

        usb_h, usb_w = usb_shape
        x_u, y_u = u_xy

        # Scale USB -> Depth pixel grid
        sx = self._depth_w / float(usb_w)
        sy = self._depth_h / float(usb_h)

        # Start with no disparity; we’ll estimate depth, then refine center with disparity
        x_center = x_u * sx
        y_center = y_u * sy

        # Helper to sample a window around (xc, yc) and return median valid depth
        def sample_depth_at(xc: float, yc: float) -> Optional[float]:
            x_d = int(round(xc))
            y_d = int(round(yc))
            x0 = max(0, x_d - win_radius)
            x1 = min(self._depth_w, x_d + win_radius + 1)
            y0 = max(0, y_d - win_radius)
            y1 = min(self._depth_h, y_d + win_radius + 1)
            if x0 >= x1 or y0 >= y1:
                return None

            depth_win = self._depth[y0:y1, x0:x1]
            conf_win  = self._conf[y0:y1, x0:x1]
            valid = (
                (conf_win >= self.confidence_min)
                & (depth_win > 0)
                & (depth_win <= Z_MAX)
            )
            if not np.any(valid):
                return None
            return float(np.median(depth_win[valid]))

        Z_mm: Optional[float] = None
        xc, yc = x_center, y_center

        for _ in range(max_iters):
            # 1) Sample depth at current center
            Z_est = sample_depth_at(xc, yc)
            if Z_est is None:
                # If we fail on the first pass, just give up
                if Z_mm is None:
                    return None
                # If we had a previous Z, keep it
                break

            Z_mm = Z_est

            # 2) Compute disparity (in DEPTH pixels) and update the window center
            # d = fx * baseline / Z
            # Note: clamp Z to avoid division by zero
            # given: Z_mm (from your window), fy_depth_px, baseline_mm (vertical), disparity_sign = +1/-1
            d_py = (fy_depth_px * baseline_mm) / max(Z_mm, 1e-6)  # disparity in DEPTH pixels
            shift_x = 0.0
            shift_y = disparity_sign * d_py + cy_offset_px  # optional constant offset in DEPTH px


            # 3) Update center for next iteration
            xc = x_center + shift_x
            yc = y_center + shift_y

        return Z_mm
