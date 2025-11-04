import json
import numpy as np
import cv2
from pathlib import Path

from typing import Any, Dict, List, Tuple, Optional, Union
import numpy.typing as npt

CONFIG_PATH = Path(__file__).resolve().parents[3] / "perpetual_os1" / "src" / "perpetual_os1" / "config.json"

def try_find_board(gray: np.ndarray, sizes: List[Tuple[int, int]]) -> Tuple[Optional[Tuple[int, int]], Optional[npt.NDArray[np.float32]]]:
    """
    Try a list of candidate inner-corner sizes with the SB detector.
    
    Args:
        gray: Grayscale input image
        sizes: List of (width, height) pattern sizes to try
        
    Returns:
        Tuple of (pattern_size, corners) on success, else (None, None)
        
    Raises:
        ValueError: If input parameters are invalid
        RuntimeError: If image processing fails
    """
    if not isinstance(gray, np.ndarray) or gray.ndim != 2:
        raise ValueError("Input must be 2D grayscale image")
        
    if not sizes:
        raise ValueError("Must provide at least one pattern size")
        
    try:
        # Optional contrast boost helps printed boards
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        g = clahe.apply(gray)

        for ps in sizes:
            try:
                if not isinstance(ps, tuple) or len(ps) != 2:
                    continue  # Skip invalid sizes
                    
                ok, corners = cv2.findChessboardCornersSB(
                    g, ps,
                    flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
                )
            except AttributeError:
                # Fallback for older OpenCV: use classic detector
                ok, corners = cv2.findChessboardCorners(
                    g, ps,
                    flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_FAST_CHECK
                )
                if ok:
                    try:
                        corners = cv2.cornerSubPix(
                            g, corners, (11,11), (-1,-1),
                            (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-3)
                        )
                    except Exception as e:
                        raise RuntimeError(f"Corner refinement failed: {str(e)}") from e
                        
            if ok and corners is not None:
                try:
                    return ps, corners.reshape(-1,2).astype(np.float32)
                except Exception as e:
                    raise RuntimeError(f"Failed to process corner data: {str(e)}") from e
                    
        return None, None
        
    except Exception as e:
        if isinstance(e, (ValueError, RuntimeError)):
            raise
        raise RuntimeError(f"Board detection failed: {str(e)}") from e

def detect_pattern_size_and_corners(img_bgr: np.ndarray) -> Tuple[Tuple[int, int], npt.NDArray[np.float32]]:
    """
    Detect checkerboard pattern and corners.
    
    Args:
        img_bgr: BGR input image
        
    Returns:
        Tuple of (pattern_size, corners)
        
    Raises:
        ValueError: If input image is invalid
        RuntimeError: If pattern detection fails
    """
    if not isinstance(img_bgr, np.ndarray) or len(img_bgr.shape) != 3:
        raise ValueError("Input must be BGR image (3D array)")
        
    try:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

        candidates = [
            (17,10), (17,9), (16,10), (16,9), 
            (15,10), (15,9), (14,9), (14,7)
        ]
        ps, corners = try_find_board(gray, candidates)
        
        if ps is None:
            raise RuntimeError(
                "Chessboard not found with any candidate size. "
                "Check that the pattern is visible and not occluded."
            )
            
        return ps, corners
        
    except Exception as e:
        if isinstance(e, (ValueError, RuntimeError)):
            raise
        raise RuntimeError(f"Pattern detection failed: {str(e)}") from e

def get_image_files() -> List[Path]:
    """
    Get list of image files from img directory.
    
    Returns:
        List of paths to image files
        
    Raises:
        FileNotFoundError: If no images found or directory doesn't exist
        RuntimeError: If directory access fails
    """
    try:
        base = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()
        img_dir = base / "img"
        
        if not img_dir.exists():
            raise FileNotFoundError(f"Image directory not found: {img_dir}")
            
        if not img_dir.is_dir():
            raise RuntimeError(f"Path is not a directory: {img_dir}")
            
        exts = {".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"}
        files = sorted(p for p in img_dir.glob("*") if p.suffix in exts)
        
        if not files:
            raise FileNotFoundError(
                f"No image files found under {img_dir} (CWD={Path.cwd()})"
            )
            
        return files
        
    except Exception as e:
        if isinstance(e, (FileNotFoundError, RuntimeError)):
            raise
        raise RuntimeError(f"Failed to get image files: {str(e)}") from e

def get_undistortion(pattern_size: Tuple[int, int] = (14,7), square_size: float = 10.0) -> Tuple[List[npt.NDArray[np.float32]], List[npt.NDArray[np.float32]], Optional[Tuple[int, int]]]:
    """
    Calculate camera undistortion parameters from calibration images.
    
    Args:
        pattern_size: Number of inner corners (width, height)
        square_size: Size of each square in millimeters
        
    Returns:
        Tuple of (object_points_3D, image_points_2D, image_size)
        
    Raises:
        ValueError: If parameters are invalid
        RuntimeError: If calibration fails or no valid images found
        FileNotFoundError: If calibration images not found
    """
    if not isinstance(pattern_size, tuple) or len(pattern_size) != 2:
        raise ValueError("pattern_size must be (width, height) tuple")
        
    if not isinstance(square_size, (int, float)) or square_size <= 0:
        raise ValueError("square_size must be positive number")
        
    try:
        objp = np.zeros((pattern_size[0]*pattern_size[1], 3), np.float32)
        objp[:, :2] = np.indices(pattern_size).T.reshape(-1, 2)  # (x,y) grid
        objp *= square_size

        obj_points_3D = []  # list of (N,3) float32
        img_points_2D = []  # list of (N,1,2) float32
        image_size = None

        criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3)

        for fn in get_image_files():
            try:
                img = cv2.imread(str(fn), cv2.IMREAD_GRAYSCALE)
                if img is None:
                    print(f"Skip unreadable: {fn}")
                    continue

                if image_size is None:
                    image_size = (img.shape[1], img.shape[0])  # (w,h)
                elif image_size != (img.shape[1], img.shape[0]):
                    print(f"Skip {fn}: inconsistent image size {img.shape[::-1]}")
                    continue

                ok, corners = cv2.findChessboardCorners(
                    img, pattern_size,
                    flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE
                )
                if not ok:
                    continue

                try:
                    corners = cv2.cornerSubPix(img, corners, (11,11), (-1,-1), criteria)
                    obj_points_3D.append(objp.copy())
                    img_points_2D.append(corners.astype(np.float32))
                except Exception as e:
                    print(f"Skip {fn}: corner refinement failed: {str(e)}")
                    continue
                    
            except Exception as e:
                print(f"Skip {fn}: processing failed: {str(e)}")
                continue

        if not obj_points_3D:
            raise RuntimeError("No valid checkerboard patterns found in any image")
            
        return obj_points_3D, img_points_2D, image_size
        
    except Exception as e:
        if isinstance(e, (ValueError, RuntimeError, FileNotFoundError)):
            raise
        raise RuntimeError(f"Calibration failed: {str(e)}") from e

def rectify_from_chessboard(
    img_u: np.ndarray, 
    pattern_size: Optional[Tuple[int, int]] = None, 
    square_size_mm: float = 10.0, 
    px_per_mm: float = 10.0
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Tuple[int, int]]:
    """
    Rectify image using detected checkerboard pattern.
    
    Args:
        img_u: Input image
        pattern_size: Optional checkerboard size (width, height)
        square_size_mm: Size of checkerboard squares in mm
        px_per_mm: Output resolution in pixels per mm
        
    Returns:
        Tuple of (rectified_image, homography_matrix, inliers, pattern_size)
        
    Raises:
        ValueError: If input parameters are invalid
        RuntimeError: If rectification fails
    """
    if not isinstance(img_u, np.ndarray) or len(img_u.shape) != 3:
        raise ValueError("Input must be BGR image (3D array)")
        
    if not isinstance(square_size_mm, (int, float)) or square_size_mm <= 0:
        raise ValueError("square_size_mm must be positive number")
        
    if not isinstance(px_per_mm, (int, float)) or px_per_mm <= 0:
        raise ValueError("px_per_mm must be positive number")
    
    try:
        # Detect checkerboard
        if pattern_size is None:
            pattern_size, pts_img = detect_pattern_size_and_corners(img_u)
        else:
            if not isinstance(pattern_size, tuple) or len(pattern_size) != 2:
                raise ValueError("pattern_size must be (width, height) tuple")
                
            gray = cv2.cvtColor(img_u, cv2.COLOR_BGR2GRAY)
            ps, pts_img = try_find_board(gray, [pattern_size])
            if ps is None:
                raise RuntimeError(
                    f"Checkerboard ({pattern_size[0]}x{pattern_size[1]}) not found"
                )

        cols, rows = pattern_size

        # Build metric destination points
        i_idx, j_idx = np.meshgrid(np.arange(cols), np.arange(rows))
        pts_world = np.column_stack([i_idx.ravel(), j_idx.ravel()]).astype(np.float32)
        pts_dst_mm = pts_world * square_size_mm

        # Calculate homography
        H, inliers = cv2.findHomography(
            pts_img, pts_dst_mm, 
            method=cv2.RANSAC, 
            ransacReprojThreshold=1.5
        )
        if H is None:
            raise RuntimeError("Failed to find homography transformation")

        # Calculate output dimensions and transform
        W_mm = (cols - 1) * square_size_mm
        H_mm = (rows - 1) * square_size_mm
        out_size = (
            int(round(W_mm * px_per_mm)), 
            int(round(H_mm * px_per_mm))
        )

        S = np.array([
            [px_per_mm, 0, 0],
            [0, px_per_mm, 0],
            [0, 0, 1]
        ], dtype=np.float32)
        H_px = S @ H  # image -> plane-in-pixels

        rectified = cv2.warpPerspective(
            img_u, H_px, out_size,
            flags=cv2.INTER_LINEAR
        )
        return rectified, H, inliers, pattern_size
        
    except Exception as e:
        if isinstance(e, (ValueError, RuntimeError)):
            raise
        raise RuntimeError(f"Image rectification failed: {str(e)}") from e

def rectify_with_H(
    img_u: np.ndarray,
    H_img2mm: np.ndarray,
    plane_W_mm: float,
    plane_H_mm: float,
    px_per_mm: float = 5.0
) -> np.ndarray:
    """
    Rectify image using a pre-computed homography matrix.
    
    Args:
        img_u: Input image
        H_img2mm: Image-to-millimeter homography matrix
        plane_W_mm: Output plane width in mm
        plane_H_mm: Output plane height in mm
        px_per_mm: Output resolution in pixels per mm
        
    Returns:
        Rectified image
        
    Raises:
        ValueError: If input parameters are invalid
        RuntimeError: If rectification fails
    """
    if not isinstance(img_u, np.ndarray) or len(img_u.shape) != 3:
        raise ValueError("Input must be BGR image (3D array)")
        
    if not isinstance(H_img2mm, np.ndarray) or H_img2mm.shape != (3, 3):
        raise ValueError("H_img2mm must be 3x3 homography matrix")
        
    if not all(isinstance(x, (int, float)) and x > 0 
              for x in (plane_W_mm, plane_H_mm, px_per_mm)):
        raise ValueError("Dimensions and px_per_mm must be positive numbers")
        
    try:
        # Scale transformation matrix
        S = np.array([
            [px_per_mm, 0, 0],
            [0, px_per_mm, 0],
            [0, 0, 1]
        ], dtype=np.float32)
        H_img2px = S @ H_img2mm  # image -> plane pixels

        # Calculate output dimensions
        out_w = int(round(plane_W_mm * px_per_mm))
        out_h = int(round(plane_H_mm * px_per_mm))

        # Apply transformation
        rectified = cv2.warpPerspective(
            img_u, H_img2px, (out_w, out_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT, 
            borderValue=0
        )
        return rectified
        
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise RuntimeError(f"Image rectification failed: {str(e)}") from e

def order_quad(pts: np.ndarray) -> np.ndarray:
    """
    Order quadrilateral points in clockwise order from top-left.
    
    Args:
        pts: Array of 4 points (x,y)
        
    Returns:
        Ordered points [top-left, top-right, bottom-right, bottom-left]
        
    Raises:
        ValueError: If input is not 4 points
        RuntimeError: If ordering fails
    """
    try:
        if not isinstance(pts, np.ndarray) or pts.shape != (4, 2):
            raise ValueError("Input must be array of 4 2D points")
            
        pts = np.array(pts, dtype=np.float32)
        s = pts.sum(axis=1)
        d = np.diff(pts, axis=1).ravel()
        
        tl = pts[np.argmin(s)]
        br = pts[np.argmax(s)]
        tr = pts[np.argmin(d)]
        bl = pts[np.argmax(d)]
        
        return np.array([tl, tr, br, bl], dtype=np.float32)
        
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise RuntimeError(f"Failed to order quad points: {str(e)}") from e

def order_corners(pts: np.ndarray) -> np.ndarray:
    """
    Order corner points in clockwise order from top-left.
    
    Args:
        pts: Array of 4 points (x,y)
        
    Returns:
        Ordered points [top-left, top-right, bottom-right, bottom-left]
        
    Raises:
        ValueError: If input is not 4 points
        RuntimeError: If ordering fails
    """
    try:
        pts = np.asarray(pts)
        if pts.size != 8:  # 4 points x 2 coordinates
            raise ValueError("Input must contain exactly 4 2D points")
            
        pts = pts.reshape(-1, 2).astype(np.float32)
        s = pts.sum(axis=1)
        diff = np.diff(pts, axis=1).ravel()
        
        ordered = np.zeros((4, 2), dtype=np.float32)
        ordered[0] = pts[np.argmin(s)]    # top-left
        ordered[2] = pts[np.argmax(s)]    # bottom-right
        ordered[1] = pts[np.argmin(diff)]  # top-right
        ordered[3] = pts[np.argmax(diff)]  # bottom-left
        
        return ordered
        
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise RuntimeError(f"Failed to order corner points: {str(e)}") from e

def is_right_angle_quad(pts: np.ndarray, cos_tol: float = 0.25) -> bool:
    """
    Check if quadrilateral has approximately right angles.
    
    Args:
        pts: Array of 4 points (x,y)
        cos_tol: Cosine tolerance for right angle check
        
    Returns:
        True if all angles are approximately 90 degrees
        
    Raises:
        ValueError: If input is invalid
        RuntimeError: If angle check fails
    """
    if not isinstance(pts, np.ndarray) or pts.shape != (4, 2):
        raise ValueError("Input must be array of 4 2D points")
        
    if not isinstance(cos_tol, (int, float)) or cos_tol <= 0:
        raise ValueError("cos_tol must be positive number")
        
    try:
        pts = order_corners(pts)
        v = np.roll(pts, -1, axis=0) - pts
        v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)
        dots = [abs(np.dot(v[i], v[(i+1) % 4])) for i in range(4)]
        return all(d <= cos_tol for d in dots)
        
    except Exception as e:
        if isinstance(e, ValueError):
            raise
        raise RuntimeError(f"Failed to check quad angles: {str(e)}") from e


def _to_serializable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (list, tuple)):
        return [_to_serializable(v) for v in value]
    return value


def persist_poe_camera_calibration(
    calibration: Dict[str, Any],
    config_path: Optional[Union[str, Path]] = None
) -> None:
    """
    Persist updated PoE camera calibration data back into config.json.
    """
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found at {path}")

    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    try:
        poe_camera = data["robot"]["tool"]["poe_camera"]
    except KeyError as exc:
        raise RuntimeError("Invalid configuration structure; expected robot.tool.poe_camera") from exc

    skip_keys = {"map1", "map2"}
    for key, value in calibration.items():
        if key in skip_keys:
            continue
        poe_camera[key] = _to_serializable(value)

    for key in skip_keys:
        poe_camera.pop(key, None)

    poe_camera["calibration_status"] = "calibrated"

    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
