import numpy as np
import cv2
from pathlib import Path

from typing import List, Tuple, Optional
import numpy.typing as npt

def try_find_board(gray: np.ndarray, sizes: List[Tuple[int, int]]) -> Tuple[Optional[Tuple[int, int]], Optional[npt.NDArray[np.float32]]]:
    """
    Try a list of candidate inner-corner sizes with the SB detector.
    Returns (pattern_size, corners) on success, else (None, None).
    """
    # Optional contrast boost helps printed boards
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    g = clahe.apply(gray)

    for ps in sizes:
        try:
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
                corners = cv2.cornerSubPix(g, corners, (11,11), (-1,-1),
                                           (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-3))
        if ok:
            return ps, corners.reshape(-1,2).astype(np.float32)
    return None, None

def detect_pattern_size_and_corners(img_bgr):
    """
    Guesses the board size from a short, plausible list.
    Tune the list to your print (inner corners!).
    """
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    candidates = [
        (17,10), (17,9), (16,10), (16,9), 
        (15,10), (15,9), (14,9), (14,7)
    ]
    ps, corners = try_find_board(gray, candidates)
    if ps is None:
        raise RuntimeError("Chessboard not found with any candidate size. Check that the full pattern is visible and not occluded.")
    return ps, corners

def get_image_files():
    base = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()
    img_dir = base / "img"
    exts = {".jpg", ".jpeg", ".png", ".JPG", ".JPEG", ".PNG"}
    files = sorted(p for p in img_dir.glob("*") if p.suffix in exts)
    if not files:
        raise FileNotFoundError(f"No images under {img_dir} (CWD={Path.cwd()})")
    return files

def get_undistortion(pattern_size=(14,7), square_size=10):
    objp = np.zeros((pattern_size[0]*pattern_size[1], 3), np.float32)
    objp[:, :2] = np.indices(pattern_size).T.reshape(-1, 2)  # (x,y) grid
    objp *= square_size

    obj_points_3D = []  # list of (N,3) float32
    img_points_2D = []  # list of (N,1,2) float32
    image_size = None

    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3)

    for fn in get_image_files():
        img = cv2.imread(str(fn), cv2.IMREAD_GRAYSCALE)
        if img is None:
            print(f"Skip unreadable: {fn}")
            continue

        if image_size is None:
            image_size = (img.shape[1], img.shape[0])    # (w,h)
        elif image_size != (img.shape[1], img.shape[0]):
            print(f"Skip {fn}: inconsistent image size {img.shape[::-1]}")
            continue

        ok, corners = cv2.findChessboardCorners(
            img, pattern_size,
            flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE
        )
        if not ok:
            continue

        corners = cv2.cornerSubPix(img, corners, (11,11), (-1,-1), criteria)
        obj_points_3D.append(objp.copy())
        img_points_2D.append(corners.astype(np.float32))

    if not obj_points_3D:
        raise RuntimeError("No chessboard corners found in any image.")
    return obj_points_3D, img_points_2D, image_size

def rectify_from_chessboard(img_u, pattern_size=None, square_size_mm=10, px_per_mm=10):
    """
    If pattern_size is None, detect it. Uses SB detector if available.
    """
    if pattern_size is None:
        pattern_size, pts_img = detect_pattern_size_and_corners(img_u)
    else:
        # use given size but still run a robust detection
        gray = cv2.cvtColor(img_u, cv2.COLOR_BGR2GRAY)
        ps, pts_img = try_find_board(gray, [pattern_size])
        if ps is None:
            raise RuntimeError(f"Chessboard ({pattern_size[0]}x{pattern_size[1]}) not found.")

    cols, rows = pattern_size

    # Build metric destination for each inner corner: (i, j) * square_size_mm
    i_idx, j_idx = np.meshgrid(np.arange(cols), np.arange(rows))
    pts_world = np.column_stack([i_idx.ravel(), j_idx.ravel()]).astype(np.float32)
    pts_dst_mm = pts_world * square_size_mm

    H, inliers = cv2.findHomography(pts_img, pts_dst_mm, method=cv2.RANSAC, ransacReprojThreshold=1.5)
    if H is None:
        raise RuntimeError("findHomography failed")

    W_mm = (cols - 1) * square_size_mm
    H_mm = (rows - 1) * square_size_mm
    out_size = (int(round(W_mm * px_per_mm)), int(round(H_mm * px_per_mm)))

    S = np.array([[px_per_mm, 0, 0],
                  [0, px_per_mm, 0],
                  [0, 0, 1]], dtype=np.float32)
    H_px = S @ H  # image -> plane-in-pixels

    rectified = cv2.warpPerspective(img_u, H_px, out_size, flags=cv2.INTER_LINEAR)
    return rectified, H, inliers, pattern_size

def rectify_with_H(img_u, H_img2mm, plane_W_mm, plane_H_mm, px_per_mm=5.0):
    S = np.array([[px_per_mm, 0, 0],
                  [0, px_per_mm, 0],
                  [0, 0, 1]], dtype=np.float32)        # mm -> pixels
    H_img2px = S @ H_img2mm                              # image -> plane pixels

    out_w = int(round(plane_W_mm * px_per_mm))
    out_h = int(round(plane_H_mm * px_per_mm))

    rectified = cv2.warpPerspective(
        img_u, H_img2px, (out_w, out_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT, borderValue=0
    )
    return rectified

def order_quad(pts):
    pts = np.array(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)

def order_corners(pts):
    pts = np.array(pts, dtype=np.float32).reshape(-1, 2)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]   # top-left
    ordered[2] = pts[np.argmax(s)]   # bottom-right
    ordered[1] = pts[np.argmin(diff)]# top-right
    ordered[3] = pts[np.argmax(diff)]# bottom-left
    return ordered

def is_right_angle_quad(pts, cos_tol=0.25):
    pts = order_corners(pts)
    v = np.roll(pts, -1, axis=0) - pts
    v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)
    dots = [abs(np.dot(v[i], v[(i+1) % 4])) for i in range(4)]
    return all(d <= cos_tol for d in dots)