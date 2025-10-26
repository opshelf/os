import asyncio
import time
from typing import Optional, Tuple, List, Dict, Any
import cv2
import numpy as np
import ArducamDepthCamera as ac

from hardware import iocam_helpers as ih
from hardware.exceptions import (
    CameraError, CameraConnectionError, CameraConfigError,
    ImageError, ImageProcessingError, TOFError, TOFConfigError, TOFDataError
)


# ============================ Async Sizer Class ============================

from typing import Optional

class AFSCam:
    """
    Async wrapper for TOF + USB sizing with optional on-screen visualization.
    - await start()
    - await measure(n_frames=5, display=True, bounding_box=(x0,y0,x1,y1))
    - await stop()
    """

    def __init__(self, usb_params, tof_params) -> None:
        # TOF parameters
        self.preferred_tof_port = None
        self.max_distance_mm = tof_params.max_distance_mm

        # Calibration & geometry
        self.square_mm = usb_params.square_mm
        self.fx_px = usb_params.fx_px
        self.fy_px = usb_params.fy_px
        self.px_per_mm = usb_params.px_per_mm
        self.confidence_min = tof_params.confidence_min

        # USB camera configuration
        self._usb_index = usb_params.usb_index
        self._usb_fourcc = usb_params.usb_fourcc
        self._usb_exposure = usb_params.usb_exposure

        # Hardware handles
        self._tof: Optional[ac.ArducamCamera] = None
        self._usb: Optional[cv2.VideoCapture] = None

        # Latest TOF frame buffers (updated in background loop)
        self._depth: Optional[np.ndarray] = None  # uint16 mm
        self._conf: Optional[np.ndarray] = None   # uint8
        self._depth_h: int = 0
        self._depth_w: int = 0

        # Calibration caches
        self._px_per_mm: Optional[float] = self.px_per_mm
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
        try:
            self._tof = ac.ArducamCamera()
            port_used = self._open_tof(self._tof, self.preferred_tof_port)
            if port_used < 0:
                raise TOFError(f"Failed to open TOF on port {self.preferred_tof_port}")
                
            if self._tof.start(ac.FrameType.DEPTH) != 0:
                self._tof.close()
                raise TOFError("Failed to start TOF stream")
                
            try:
                self._tof.setControl(ac.Control.RANGE, self.max_distance_mm)
            except Exception as e:
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
            
    def _init_usb(self) -> None:
        """Initialize USB camera."""
        try:
            self._usb = cv2.VideoCapture(self._usb_index, cv2.CAP_V4L2)
            if not self._usb.isOpened():
                raise CameraConnectionError(
                    f"Failed to open USB camera at index {self._usb_index}"
                )

            # Configure camera settings
            if self._usb_fourcc:
                try:
                    fourcc = cv2.VideoWriter_fourcc(*self._usb_fourcc)
                    if not self._usb.set(cv2.CAP_PROP_FOURCC, fourcc):
                        raise CameraConfigError(
                            f"Failed to set fourcc to {self._usb_fourcc}"
                        )
                except Exception as e:
                    raise CameraConfigError(
                        f"Error setting fourcc: {str(e)}"
                    ) from e

            if self._usb_exposure is not None:
                try:
                    if not self._usb.set(cv2.CAP_PROP_EXPOSURE, float(self._usb_exposure)):
                        raise CameraConfigError(
                            f"Failed to set exposure to {self._usb_exposure}"
                        )
                except Exception as e:
                    raise CameraConfigError(
                        f"Error setting exposure: {str(e)}"
                    ) from e

            # Get initial frame and compute camera parameters
            ok, frame0 = self._usb.read()
            if not ok or frame0 is None:
                raise CameraError("Failed to get initial USB frame")
                
            if self._fx_cache is None or self._fy_cache is None:
                try:
                    h0, w0 = frame0.shape[:2]
                    if h0 <= 0 or w0 <= 0:
                        raise ValueError(f"Invalid frame dimensions: {w0}x{h0}")
                        
                    fx, fy = ih.compute_fx_fy_from_fov(
                        w0, h0, self.hfov_deg, self.vfov_deg
                    )
                    self._fx_cache = self._fx_cache or fx
                    self._fy_cache = self._fy_cache or fy
                        
                except Exception as e:
                    raise CameraConfigError(
                        f"Failed to compute camera parameters: {str(e)}"
                    ) from e
                    
        except Exception as e:
            self._cleanup_usb()
            if not isinstance(e, (CameraConnectionError, CameraConfigError)):
                raise CameraError(
                    f"USB camera initialization failed: {str(e)}"
                ) from e
            raise
            
    async def start(self) -> None:
        """
        Initialize and start both TOF and USB cameras.
        
        Raises:
            TOFError: If TOF camera initialization or configuration fails
            CameraConnectionError: If USB camera connection fails
            CameraConfigError: If camera configuration fails
            CameraError: If general camera initialization fails
        """
        try:
            # Initialize TOF camera first
            # self._init_tof()
            
            # Initialize USB camera
            self._init_usb()
            
            # Start background TOF loop
            # self._stop_evt.clear()
            # self._loop_task = asyncio.create_task(self._tof_loop())
            
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

    def measure_sync(
        self,
        n_frames: int = 5,
        timeout_s: float = 2.0,
        display: bool = False,
        window_name: str = "usb_sizing",
        bounding_box: Optional[Tuple[int, int, int, int]] = (200,60,408,265),  # (x0, y0, x1, y1)
        bbox_inclusive: bool = True,
        max_center_offset_px: float = 40.0
    ) -> Dict[str, Any]:
        """
        Synchronous variant of :meth:`measure` that draws the distance between the given
        bounding box center and the detected contour center on the USB frame.

        Args:
            n_frames: Number of frames to capture
            timeout_s: Timeout for operation
            display: Whether to show visualization
            window_name: Name for display window
            bounding_box: Region of interest as (x0, y0, x1, y1)
            bbox_inclusive: Whether bbox is inclusive
            max_center_offset_px: Maximum allowed center offset

        Returns:
            Dict containing measurement results and the last annotated frame

        Raises:
            CameraError: If USB camera is not ready
            ImageError: If frame capture fails
            ImageProcessingError: If analysis fails
            ValueError: If parameters are invalid
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

            if self._px_per_mm is None:
                gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
                det = ih.detect_checkerboard_scale_detailed(gray0, self.square_mm)

                if det is None:
                    raise ImageProcessingError("Failed to detect checkerboard scale")

                self._px_per_mm = det["px_per_mm"]

                if (self._fx_cache is None or self._fy_cache is None) and self._depth is not None:
                    try:
                        cal = self._calibrate_fov_from_checkerboard(det, gray0.shape)
                        if cal is not None:
                            fx_est, fy_est, hfov_est, vfov_est = cal
                            self._fx_cache = self._fx_cache or fx_est
                            self._fy_cache = self._fy_cache or fy_est
                            self._hfov_cal, self._vfov_cal = hfov_est, vfov_est
                    except Exception as e:
                        raise ImageProcessingError(
                            f"FOV calibration failed: {str(e)}"
                        ) from e

                if self._px_per_mm is None:
                    self._px_per_mm = 1.7  # fallback value

            per_frame: List[Dict[str, Any]] = []
            last_draw: Optional[np.ndarray] = None

            for fr in frames:
                try:
                    draw = fr.copy()
                    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

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

                    mask = ih.segment_object_on_checkerboard(gray, self._px_per_mm, self.square_mm)
                    if mask is None:
                        per_frame.append({"ok": False, "reason": "segmentation_failed"})
                        last_draw = draw
                        continue

                    contour = ih.pick_main_contour(mask)
                    if contour is None:
                        per_frame.append({"ok": False, "reason": "no_contour"})
                        last_draw = draw
                        print("no mask")
                        continue

                    w_px, h_px, box, (cx, cy) = ih.measure_contour_px(contour)
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
                            draw,
                            f"{center_dist_px:.1f}px",
                            (label_x, label_y),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            (255, 255, 0),
                            1,
                            cv2.LINE_AA,
                        )
                    else:
                        cv2.circle(draw, (int(round(cx)), int(round(cy))), 4, (0, 0, 255), -1)

                    try:
                        size_w_mm_scale = w_px / self._px_per_mm
                        size_h_mm_scale = h_px / self._px_per_mm
                        a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Scale calculation failed: {str(e)}"
                        ) from e

                    try:
                        z_mm = self._sample_depth_at_usb_point((cx, cy), gray.shape)
                        footprint = None

                        if z_mm is not None and self._fx_cache and self._fy_cache:
                            w_mm_fp, h_mm_fp = ih.footprint_from_rect_pinhole(
                                w_px, h_px, z_mm, self._fx_cache, self._fy_cache
                            )
                            a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                            footprint = (a_fp, b_fp)

                    except Exception as e:
                        raise ImageProcessingError(
                            f"Depth/footprint calculation failed: {str(e)}"
                        ) from e

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

    async def measure_sync(
        self,
        n_frames: int = 5,
        timeout_s: float = 2.0,
        display: bool = False,
        window_name: str = "usb_sizing",
        bounding_box: Optional[Tuple[int, int, int, int]] = (200,60,408,265),  # (x0, y0, x1, y1)
        bbox_inclusive: bool = True,
        max_center_offset_px: float = 40.0
    ) -> Dict[str, Any]:
        """
        Capture and analyze measurements using both cameras.
        
        Args:
            n_frames: Number of frames to capture
            timeout_s: Timeout for operation
            display: Whether to show visualization
            window_name: Name for display window
            bounding_box: Region of interest as (x0, y0, x1, y1)
            bbox_inclusive: Whether bbox is inclusive
            max_center_offset_px: Maximum allowed center offset
            
        Returns:
            Dict containing measurement results
            
        Raises:
            CameraError: If USB camera is not ready
            ImageError: If frame capture fails
            ImageProcessingError: If analysis fails
            ValueError: If parameters are invalid
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
        loop = asyncio.get_running_loop()

        try:
            # Capture frames with timeout
            capture_start = time.time()
            frames_captured = 0
            
            while frames_captured < n_frames:
                # Check timeout
                if time.time() - capture_start > timeout_s:
                    raise ImageError(f"Frame capture timeout after {frames_captured} frames")
                
                try:
                    ok, frame = await loop.run_in_executor(None, self._usb.read)
                    if not ok:
                        raise ImageError("Frame capture failed")
                    if frame is None:
                        raise ImageError("Received null frame")
                        
                    frames.append(frame)
                    frames_captured += 1
                    
                except Exception as e:
                    if not isinstance(e, ImageError):
                        raise ImageError(f"Error capturing frame: {str(e)}") from e
                    raise

            if not frames:
                if display:
                    self._show_text_frame("USB read failed", window_name)
                return {"ok": False, "reason": "usb_read_failed"}

            # Initialize measurements if needed
            if self._px_per_mm is None:
                gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
                det = ih.detect_checkerboard_scale_detailed(gray0, self.square_mm)
                
                if det is None:
                    raise ImageProcessingError("Failed to detect checkerboard scale")
                
                self._px_per_mm = det["px_per_mm"]
                
                # Calibrate FOV if needed
                if (self._fx_cache is None or self._fy_cache is None) and self._depth is not None:
                    try:
                        cal = self._calibrate_fov_from_checkerboard(det, gray0.shape)
                        if cal is not None:
                            fx_est, fy_est, hfov_est, vfov_est = cal
                            self._fx_cache = self._fx_cache or fx_est
                            self._fy_cache = self._fy_cache or fy_est
                            self._hfov_cal, self._vfov_cal = hfov_est, vfov_est
                    except Exception as e:
                        raise ImageProcessingError(f"FOV calibration failed: {str(e)}") from e
                
                if self._px_per_mm is None:
                    self._px_per_mm = 1.7  # fallback value
                    
            # Process each frame
            per_frame: List[Dict[str, Any]] = []
            last_draw = None

            for fr in frames:
                try:
                    draw = fr.copy()
                    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

                    # Segment and analyze image
                    mask = ih.segment_object_on_checkerboard(gray, self._px_per_mm, self.square_mm)
                    if mask is None:
                        raise ImageProcessingError("Segmentation failed")
                        
                    contour = ih.pick_main_contour(mask)
                    if contour is None:
                        per_frame.append({"ok": False, "reason": "no_contour"})
                        continue

                    # Calculate measurements
                    w_px, h_px, box, (cx, cy) = ih.measure_contour_px(contour)
                    box_i = np.asarray(box, dtype=int)

                    # Process bounding box if provided
                    center_dist_px = None
                    if bounding_box is not None:
                        try:
                            x0, y0, x1, y1 = map(int, bounding_box)
                            bx_cx = (x0 + x1) / 2.0
                            by_cy = (y0 + y1) / 2.0
                            center_dist_px = float(np.hypot(cx - bx_cx, cy - by_cy))
                        except Exception as e:
                            raise ImageProcessingError(
                                f"Bounding box calculation failed: {str(e)}"
                            ) from e

                    # Calculate dimensions
                    try:
                        size_w_mm_scale = w_px / self._px_per_mm
                        size_h_mm_scale = h_px / self._px_per_mm
                        a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Scale calculation failed: {str(e)}"
                        ) from e

                    # Process depth and footprint
                    try:
                        z_mm = self._sample_depth_at_usb_point((cx, cy), gray.shape)
                        footprint = None
                        
                        if z_mm is not None and self._fx_cache and self._fy_cache:
                            w_mm_fp, h_mm_fp = ih.footprint_from_rect_pinhole(
                                w_px, h_px, z_mm, self._fx_cache, self._fy_cache
                            )
                            a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                            footprint = (a_fp, b_fp)
                            
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Depth/footprint calculation failed: {str(e)}"
                        ) from e

                    # Store results for this frame
                    per_frame.append({
                        "ok": True,
                        "center_xy": (int(cx), int(cy)),
                        "center_dist_px": center_dist_px,
                        "scale_mm": (float(a_scale), float(b_scale)),
                        "depth_mm": float(z_mm) if z_mm is not None else None,
                        "footprint_mm": (float(footprint[0]), float(footprint[1])) 
                            if footprint is not None else None
                    })
                    
                except Exception as e:
                    if not isinstance(e, ImageProcessingError):
                        raise ImageProcessingError(
                            f"Frame processing failed: {str(e)}"
                        ) from e
                    raise
                    
            # Calculate aggregates from valid frames
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

                    # Add footprint medians when available
                    footprints = [p["footprint_mm"] for p in ok_frames if p["footprint_mm"] is not None]
                    if footprints:
                        agg["footprint_major_mm_med"] = med([fp[0] for fp in footprints])
                        agg["footprint_minor_mm_med"] = med([fp[1] for fp in footprints])

                    if bounding_box is not None:
                        dists = [p["center_dist_px"] for p in ok_frames if p["center_dist_px"] is not None]
                        agg["center_dist_px_med"] = med(dists)
                
                return {
                    "ok": bool(ok_frames),
                    "aggregate": agg
                }
                
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
        n_frames: int = 5,
        timeout_s: float = 2.0,
        display: bool = False,
        window_name: str = "usb_sizing",
        bounding_box: Optional[Tuple[int, int, int, int]] = (200,60,408,265),  # (x0, y0, x1, y1)
        bbox_inclusive: bool = True,
        max_center_offset_px: float = 40.0
    ) -> Dict[str, Any]:
        """
        Capture and analyze measurements using both cameras.
        
        Args:
            n_frames: Number of frames to capture
            timeout_s: Timeout for operation
            display: Whether to show visualization
            window_name: Name for display window
            bounding_box: Region of interest as (x0, y0, x1, y1)
            bbox_inclusive: Whether bbox is inclusive
            max_center_offset_px: Maximum allowed center offset
            
        Returns:
            Dict containing measurement results
            
        Raises:
            CameraError: If USB camera is not ready
            ImageError: If frame capture fails
            ImageProcessingError: If analysis fails
            ValueError: If parameters are invalid
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
        loop = asyncio.get_running_loop()

        try:
            # Capture frames with timeout
            capture_start = asyncio.get_event_loop().time()
            frames_captured = 0
            
            while frames_captured < n_frames:
                # Check timeout
                if asyncio.get_event_loop().time() - capture_start > timeout_s:
                    raise ImageError(f"Frame capture timeout after {frames_captured} frames")
                
                try:
                    ok, frame = await loop.run_in_executor(None, self._usb.read)
                    if not ok:
                        raise ImageError("Frame capture failed")
                    if frame is None:
                        raise ImageError("Received null frame")
                        
                    frames.append(frame)
                    frames_captured += 1
                    
                except Exception as e:
                    if not isinstance(e, ImageError):
                        raise ImageError(f"Error capturing frame: {str(e)}") from e
                    raise

            if not frames:
                if display:
                    self._show_text_frame("USB read failed", window_name)
                return {"ok": False, "reason": "usb_read_failed"}

            # Initialize measurements if needed
            if self._px_per_mm is None:
                gray0 = cv2.cvtColor(frames[0], cv2.COLOR_BGR2GRAY)
                det = ih.detect_checkerboard_scale_detailed(gray0, self.square_mm)
                
                if det is None:
                    raise ImageProcessingError("Failed to detect checkerboard scale")
                
                self._px_per_mm = det["px_per_mm"]
                
                # Calibrate FOV if needed
                if (self._fx_cache is None or self._fy_cache is None) and self._depth is not None:
                    try:
                        cal = self._calibrate_fov_from_checkerboard(det, gray0.shape)
                        if cal is not None:
                            fx_est, fy_est, hfov_est, vfov_est = cal
                            self._fx_cache = self._fx_cache or fx_est
                            self._fy_cache = self._fy_cache or fy_est
                            self._hfov_cal, self._vfov_cal = hfov_est, vfov_est
                    except Exception as e:
                        raise ImageProcessingError(f"FOV calibration failed: {str(e)}") from e
                
                if self._px_per_mm is None:
                    self._px_per_mm = 1.7  # fallback value
                    
            # Process each frame
            per_frame: List[Dict[str, Any]] = []
            last_draw = None

            for fr in frames:
                try:
                    draw = fr.copy()
                    gray = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)

                    # Segment and analyze image
                    mask = ih.segment_object_on_checkerboard(gray, self._px_per_mm, self.square_mm)
                    if mask is None:
                        raise ImageProcessingError("Segmentation failed")
                        
                    contour = ih.pick_main_contour(mask)
                    if contour is None:
                        per_frame.append({"ok": False, "reason": "no_contour"})
                        continue

                    # Calculate measurements
                    w_px, h_px, box, (cx, cy) = ih.measure_contour_px(contour)
                    box_i = np.asarray(box, dtype=int)

                    # Process bounding box if provided
                    center_dist_px = None
                    if bounding_box is not None:
                        try:
                            x0, y0, x1, y1 = map(int, bounding_box)
                            bx_cx = (x0 + x1) / 2.0
                            by_cy = (y0 + y1) / 2.0
                            center_dist_px = float(np.hypot(cx - bx_cx, cy - by_cy))
                        except Exception as e:
                            raise ImageProcessingError(
                                f"Bounding box calculation failed: {str(e)}"
                            ) from e

                    # Calculate dimensions
                    try:
                        size_w_mm_scale = w_px / self._px_per_mm
                        size_h_mm_scale = h_px / self._px_per_mm
                        a_scale, b_scale = sorted([size_w_mm_scale, size_h_mm_scale], reverse=True)
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Scale calculation failed: {str(e)}"
                        ) from e

                    # Process depth and footprint
                    try:
                        z_mm = self._sample_depth_at_usb_point((cx, cy), gray.shape)
                        footprint = None
                        
                        if z_mm is not None and self._fx_cache and self._fy_cache:
                            w_mm_fp, h_mm_fp = ih.footprint_from_rect_pinhole(
                                w_px, h_px, z_mm, self._fx_cache, self._fy_cache
                            )
                            a_fp, b_fp = sorted([w_mm_fp, h_mm_fp], reverse=True)
                            footprint = (a_fp, b_fp)
                            
                    except Exception as e:
                        raise ImageProcessingError(
                            f"Depth/footprint calculation failed: {str(e)}"
                        ) from e

                    # Store results for this frame
                    per_frame.append({
                        "ok": True,
                        "center_xy": (int(cx), int(cy)),
                        "center_dist_px": center_dist_px,
                        "scale_mm": (float(a_scale), float(b_scale)),
                        "depth_mm": float(z_mm) if z_mm is not None else None,
                        "footprint_mm": (float(footprint[0]), float(footprint[1])) 
                            if footprint is not None else None
                    })
                    
                except Exception as e:
                    if not isinstance(e, ImageProcessingError):
                        raise ImageProcessingError(
                            f"Frame processing failed: {str(e)}"
                        ) from e
                    raise
                    
            # Calculate aggregates from valid frames
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

                    # Add footprint medians when available
                    footprints = [p["footprint_mm"] for p in ok_frames if p["footprint_mm"] is not None]
                    if footprints:
                        agg["footprint_major_mm_med"] = med([fp[0] for fp in footprints])
                        agg["footprint_minor_mm_med"] = med([fp[1] for fp in footprints])

                    if bounding_box is not None:
                        dists = [p["center_dist_px"] for p in ok_frames if p["center_dist_px"] is not None]
                        agg["center_dist_px_med"] = med(dists)
                
                return {
                    "ok": bool(ok_frames),
                    "aggregate": agg
                }
                
            except Exception as e:
                raise ImageProcessingError(
                    f"Aggregate calculation failed: {str(e)}"
                ) from e

        except Exception as e:
            if not isinstance(e, (ImageError, ImageProcessingError)):
                raise ImageProcessingError(f"Measurement failed: {str(e)}") from e
            raise

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
