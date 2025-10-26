import numpy as np
import cv2
import time
from pathlib import Path
from typing import Dict, Optional, Tuple, Any
import numpy.typing as npt

from hardware import toolcam_helpers
from hardware.exceptions import (
    CameraError, CameraConnectionError, CameraConfigError,
    ImageError, ImageProcessingError, CalibrationError
)

MIN_AREA = 200          # reject tiny blobs (px^2)
ASPECT_TOL = 0.75       # allow ±20% from perfect square (w≈h)

class ToolCamera:
    url: str
    mtx: npt.NDArray  # camera matrix
    dist: npt.NDArray  # distortion coefficients         
    rvecs: npt.NDArray
    tvecs: npt.NDArray
    newcameramtx: npt.NDArray
    roi: Tuple[int, int, int, int]
    H: npt.NDArray  # homography to board plane

    # remap fields
    map1: npt.NDArray
    map2: npt.NDArray
    map_size: Tuple[int, int]

    H_ref_size: Tuple[int, int]
    board_W_mm: float  # physical board size
    board_H_mm: float
    pattern_size: Tuple[int, int]
    square_size_mm: float

    def __init__(self, params) -> None:
        self.url = params.url  # default to first local camera
        self.mtx = params.mtx # camera matrix
        self.dist = params.dist # distortion coefficients         
        self.rvecs = params.rvecs
        self.tvecs = params.tvecs
        self.newcameramtx = params.newcameramtx
        self.roi = params.roi
        self.H = params.H  # homography to board plane

        # remap fields
        self.map1 = params.map1
        self.map2 = params.map2
        self.map_size = params.map_size

        self.H_ref_size = params.H_ref_size
        self.board_W_mm = params.board_W_mm  # physical board size
        self.board_H_mm = params.board_H_mm
        self.pattern_size = params.pattern_size
        self.square_size_mm = params.square_size_mm

        # self.get_correct() #

    async def capture(self) -> npt.NDArray:
        """
        Capture a frame from the camera.
        
        Returns:
            numpy.ndarray: The captured frame
            
        Raises:
            CameraConnectionError: If camera cannot be opened or initialized
            CameraConfigError: If camera configuration fails
            ImageError: If frame capture fails or returns invalid data
        """
        try:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                raise CameraConnectionError(
                    f"Failed to open camera at URL: {self.url}"
                )
            
            try:
                # Configure camera settings
                if not cap.set(cv2.CAP_PROP_BUFFERSIZE, 1):
                    raise CameraConfigError("Failed to set buffer size")
                if not cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000):
                    raise CameraConfigError("Failed to set open timeout")
                if not cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000):
                    raise CameraConfigError("Failed to set read timeout")

                # Clear buffer
                t0 = time.time()
                while time.time() - t0 < 4.25:  # idr interval
                    if not cap.grab():
                        raise ImageError("Failed to grab frame during buffer clear")
                    time.sleep(0.001)

                # Grab additional frames to ensure fresh data
                for _ in range(5):
                    if not cap.grab():
                        raise ImageError("Failed to grab frame during refresh")

                # Read final frame
                ok, frame = cap.read()
                if not ok or frame is None:
                    raise ImageError(
                        "Failed to read frame or received empty frame"
                    )
                return frame
                
            finally:
                cap.release()
                
        except cv2.error as e:
            raise CameraError(f"OpenCV error: {str(e)}") from e
        except Exception as e:
            raise CameraError(f"Unexpected camera error: {str(e)}") from e
    
    def _build_remap(self, size_wh: Tuple[int, int]) -> None:
        """
        (Re)compute remap for given (w,h) using current intrinsics.
        
        Args:
            size_wh: Tuple of (width, height) for the target image size
            
        Raises:
            CalibrationError: If camera matrix computation fails
            CameraConfigError: If remapping parameters cannot be computed
        """
        try:
            w, h = size_wh
            if w <= 0 or h <= 0:
                raise ValueError(f"Invalid image dimensions: {w}x{h}")
            
            try:
                self.newcameramtx, self.roi = cv2.getOptimalNewCameraMatrix(
                    self.mtx, self.dist, (w, h), alpha=1, newImgSize=(w, h)
                )
            except cv2.error as e:
                raise CalibrationError(
                    f"Failed to compute optimal camera matrix: {str(e)}"
                ) from e
                
            # Identity rectification (no stereo R), just undistort into newcameramtx
            R = np.eye(3, dtype=np.float32)
            try:
                self.map1, self.map2 = cv2.initUndistortRectifyMap(
                    self.mtx, self.dist, R, self.newcameramtx, (w, h), cv2.CV_32FC1
                )
            except cv2.error as e:
                raise CameraConfigError(
                    f"Failed to initialize undistortion map: {str(e)}"
                ) from e
                
            self.map_size = (w, h)
            
        except Exception as e:
            if not isinstance(e, (CalibrationError, CameraConfigError)):
                raise CameraConfigError(f"Remap computation failed: {str(e)}") from e
            raise

    def get_correct(self):
        """
        Calibrate camera intrinsics using chessboard pattern.
        
        Raises:
            CalibrationError: If calibration pattern detection or parameter computation fails
            ImageProcessingError: If image processing during calibration fails
        """
        try:
            # Get calibration data
            try:
                obj3d, img2d, img_sz = toolcam_helpers.get_undistortion(
                    self.pattern_size,
                    self.square_size_mm
                )
            except Exception as e:
                raise CalibrationError(
                    f"Failed to get calibration pattern data: {str(e)}"
                ) from e
            
            # Calibrate camera
            try:
                ret, self.mtx, self.dist, self.rvecs, self.tvecs = cv2.calibrateCamera(
                    obj3d, img2d, img_sz, None, None
                )
                if not ret:
                    raise CalibrationError("Camera calibration failed - invalid result")
            except cv2.error as e:
                raise CalibrationError(f"Camera calibration failed: {str(e)}") from e

            # Load and process reference image for homography
            try:
                base = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()
                img_path = base / "img" / "1.jpg"     # image used to compute H
                img = cv2.imread(str(img_path))
                if img is None:
                    raise ImageError(f"Failed to load calibration image: {img_path}")
                
                h0, w0 = img.shape[:2]
                if h0 <= 0 or w0 <= 0:
                    raise ImageError(f"Invalid image dimensions: {w0}x{h0}")
                
                # Build remap and undistort
                self._build_remap((w0, h0))
                
                try:
                    undist0 = cv2.remap(img, self.map1, self.map2, 
                                      interpolation=cv2.INTER_LINEAR)
                except cv2.error as e:
                    raise ImageProcessingError(
                        f"Failed to undistort calibration image: {str(e)}"
                    ) from e

                # Compute homography from chessboard
                try:
                    rectified0, self.H, _, detected_ps = toolcam_helpers.rectify_from_chessboard(
                        undist0, pattern_size=None, 
                        square_size_mm=self.square_size_mm, px_per_mm=10
                    )
                    if self.H is None:
                        raise CalibrationError("Failed to compute homography matrix")
                        
                    self.pattern_size = detected_ps  # remember what we actually have
                    if not detected_ps or len(detected_ps) != 2:
                        raise CalibrationError(
                            f"Invalid pattern size detected: {detected_ps}"
                        )

                    # Calculate board dimensions
                    cols, rows = self.pattern_size
                    if cols < 2 or rows < 2:
                        raise CalibrationError(
                            f"Invalid pattern dimensions: {cols}x{rows}"
                        )
                    self.board_W_mm = (cols - 1) * self.square_size_mm
                    self.board_H_mm = (rows - 1) * self.square_size_mm

                    try:
                        toolcam_helpers.persist_poe_camera_calibration(
                            {
                                "mtx": self.mtx,
                                "dist": self.dist,
                                "rvecs": self.rvecs,
                                "tvecs": self.tvecs,
                                "newcameramtx": self.newcameramtx,
                                "roi": self.roi,
                                "H": self.H,
                                "map_size": self.map_size,
                                "H_ref_size": self.H_ref_size,
                                "board_W_mm": self.board_W_mm,
                                "board_H_mm": self.board_H_mm,
                                "pattern_size": self.pattern_size,
                                "square_size_mm": self.square_size_mm,
                            }
                        )
                    except Exception as e:
                        raise CalibrationError(
                            f"Failed to persist calibration data: {str(e)}"
                        ) from e
                    
                except Exception as e:
                    if not isinstance(e, CalibrationError):
                        raise CalibrationError(
                            f"Failed to compute homography: {str(e)}"
                        ) from e
                    raise
                    
            except Exception as e:
                if not isinstance(e, (ImageError, CalibrationError, ImageProcessingError)):
                    raise CalibrationError(
                        f"Unexpected error during calibration: {str(e)}"
                    ) from e
                raise

        except Exception as e:
            if not isinstance(e, (CalibrationError, ImageError, ImageProcessingError)):
                raise CalibrationError(f"Calibration failed: {str(e)}") from e
            raise


    def correct(self, img: npt.NDArray) -> npt.NDArray:
        """
        Undistort with remap, then rectify with H scaled to the current size.
        
        Args:
            img: Input image to correct
            
        Returns:
            numpy.ndarray: Corrected and rectified image
            
        Raises:
            ImageProcessingError: If image processing or rectification fails
            CalibrationError: If correction matrices are not properly initialized
        """
        try:
            if img is None:
                raise ValueError("Input image is None")
                
            if len(img.shape) < 2:
                raise ValueError(f"Invalid image dimensions: {img.shape}")
                
            h, w = img.shape[:2]
            if h <= 0 or w <= 0:
                raise ValueError(f"Invalid image dimensions: {w}x{h}")

            # Ensure calibration matrices are initialized
            if self.H is None:
                raise CalibrationError(
                    "Homography matrix not initialized - run calibration first"
                )
                
            # Update remap if needed
            if self.map1 is None or self.map_size != (w, h):
                self._build_remap((w, h))

            # Undistort
            try:
                undist = cv2.remap(img, self.map1, self.map2, 
                                 interpolation=cv2.INTER_LINEAR)
            except cv2.error as e:
                raise ImageProcessingError(
                    f"Failed to undistort image: {str(e)}"
                ) from e

            # Scale homography matrix
            try:
                w0, h0 = self.H_ref_size
                if (w, h) != (w0, h0):
                    S_in = np.array([[w0 / w, 0, 0],
                                   [0, h0 / h, 0],
                                   [0,     0, 1]], dtype=np.float32)
                    H_use = self.H @ S_in
                else:
                    H_use = self.H

                # Rectify using scaled homography
                rectified = toolcam_helpers.rectify_with_H(
                    undist, H_use,
                    plane_W_mm=self.board_W_mm,
                    plane_H_mm=self.board_H_mm,
                    px_per_mm=10
                )
                
                if rectified is None:
                    raise ImageProcessingError("Rectification failed - null result")
                    
                return rectified
                
            except Exception as e:
                if not isinstance(e, ImageProcessingError):
                    raise ImageProcessingError(
                        f"Failed to rectify image: {str(e)}"
                    ) from e
                raise
                
        except Exception as e:
            if not isinstance(e, (ImageProcessingError, CalibrationError)):
                raise ImageProcessingError(
                    f"Image correction failed: {str(e)}"
                ) from e
            raise

    def get_center(
        self, 
        frame: npt.NDArray,
        object_bbox: Tuple[int, int, int, int]
    ) -> Tuple[float, float]:
        """
        Calculate center offset between frame and object.
        
        Args:
            frame: Input image
            object_bbox: (x, y, w, h) of detected object
            
        Returns:
            Tuple[float, float]: (cx, cy) offset from frame center to object center
            
        Raises:
            ValueError: If frame or bbox parameters are invalid
        """
        try:
            if frame is None:
                raise ValueError("Input frame is None")
                
            if len(frame.shape) < 2:
                raise ValueError(
                    f"Invalid frame dimensions: {frame.shape}"
                )
                
            frame_h, frame_w = frame.shape[:2]
            if frame_h <= 0 or frame_w <= 0:
                raise ValueError(f"Invalid frame size: {frame_w}x{frame_h}")
            
            if object_bbox is None:
                raise ValueError("Object bbox is None")
                
            if len(object_bbox) != 4:
                raise ValueError(
                    f"Invalid bbox dimensions: expected 4, got {len(object_bbox)}"
                )

            x, y, w, h = object_bbox
            if w <= 0 or h <= 0:
                raise ValueError(f"Invalid bbox dimensions: {w}x{h}")
            
            # Calculate centers
            frame_center = (frame_w / 2, frame_h / 2)
            object_center = (x + w / 2, y + h / 2)

            # Calculate offsets
            cx = frame_center[0] - object_center[0] 
            cy = frame_center[1] - object_center[1]

            return cx, cy
            
        except Exception as e:
            if not isinstance(e, ValueError):
                raise ValueError(f"Center calculation failed: {str(e)}") from e
            raise

    def detect_white_squares(
        self, 
        frame: npt.NDArray
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        Detect white squares in the input frame.
        
        Args:
            frame: Input BGR image
            
        Returns:
            Optional[Tuple[int, int, int, int]]: (x, y, w, h) of best square or None if none found
            
        Raises:
            ImageProcessingError: If image processing or detection fails
            ValueError: If input frame is invalid
        """
        try:
            if frame is None:
                raise ValueError("Input frame is None")
                
            if len(frame.shape) != 3:
                raise ValueError(
                    f"Invalid frame dimensions: expected 3, got {len(frame.shape)}"
                )

            try:
                # Convert to grayscale and preprocess
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                gray = cv2.GaussianBlur(gray, (3, 3), 0)
                
                # Threshold to binary
                _, bin_ = cv2.threshold(gray, 225, 255, cv2.THRESH_BINARY)
                
                # Morphological operations
                kernel = np.ones((3, 3), np.uint8)
                bin_ = cv2.morphologyEx(bin_, cv2.MORPH_OPEN, kernel, iterations=1)
                
                # Find contours
                contours, _ = cv2.findContours(
                    bin_, 
                    cv2.RETR_EXTERNAL, 
                    cv2.CHAIN_APPROX_SIMPLE
                )
                
            except cv2.error as e:
                raise ImageProcessingError(
                    f"Image preprocessing failed: {str(e)}"
                ) from e

            # Process contours to find valid squares
            valid_rects = []
            for cnt in contours:
                try:
                    # Check area
                    area = cv2.contourArea(cnt)
                    if area < MIN_AREA:
                        continue
                        
                    # Polygonal approximation
                    eps = 0.02 * cv2.arcLength(cnt, True)
                    approx = cv2.approxPolyDP(cnt, eps, True)
                    
                    # Validate shape
                    if len(approx) != 4 or not cv2.isContourConvex(approx):
                        continue
                        
                    # Check angles
                    pts = approx.reshape(-1, 2).astype(np.float32)
                    if not toolcam_helpers.is_right_angle_quad(pts):
                        continue

                    # Get bounding rectangle
                    x, y, w, h = cv2.boundingRect(approx)
                    if w == 0 or h == 0:
                        continue
                        
                    # Check aspect ratio
                    aspect = w / float(h)
                    if not (1.0 - ASPECT_TOL <= aspect <= 1.0 + ASPECT_TOL):
                        continue

                    valid_rects.append((x, y, w, h))
                    
                except Exception as e:
                    # Skip problematic contours but continue processing
                    continue

            # Return best rectangle
            if not valid_rects:
                return None

            return max(valid_rects, key=lambda b: b[2] * b[3])
            
        except Exception as e:
            if not isinstance(e, (ImageProcessingError, ValueError)):
                raise ImageProcessingError(
                    f"Square detection failed: {str(e)}"
                ) from e
            raise
