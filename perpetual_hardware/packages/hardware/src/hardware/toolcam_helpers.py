import numpy as np
import cv2


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
        S = np.array([
            [px_per_mm, 0, 0],
            [0, px_per_mm, 0],
            [0, 0, 1]
        ], dtype=np.float32)
        H_img2px = S @ H_img2mm

        out_w = int(round(plane_W_mm * px_per_mm))
        out_h = int(round(plane_H_mm * px_per_mm))

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
        if pts.size != 8:
            raise ValueError("Input must contain exactly 4 2D points")
            
        pts = pts.reshape(-1, 2).astype(np.float32)
        s = pts.sum(axis=1)
        diff = np.diff(pts, axis=1).ravel()
        
        ordered = np.zeros((4, 2), dtype=np.float32)
        ordered[0] = pts[np.argmin(s)]
        ordered[2] = pts[np.argmax(s)]
        ordered[1] = pts[np.argmin(diff)]
        ordered[3] = pts[np.argmax(diff)]
        
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
