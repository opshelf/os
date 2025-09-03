import glob, cv2, numpy as np
from pathlib import Path

def rectify_from_chessboard(img_u, pattern_size, square_size_mm, px_per_mm=5.0):
    """
    img_u: undistorted image
    pattern_size: (cols, rows) of INNER corners passed to findChessboardCorners
    square_size_mm: physical size of ONE square (not corner spacing) in mm
    px_per_mm: output pixel density

    Output image spans (cols-1)*square_size by (rows-1)*square_size in mm.
    """
    cols, rows = pattern_size
    gray = cv2.cvtColor(img_u, cv2.COLOR_BGR2GRAY)

    ok, corners = cv2.findChessboardCorners(
        gray, pattern_size,
        flags=cv2.CALIB_CB_ADAPTIVE_THRESH | cv2.CALIB_CB_NORMALIZE_IMAGE
    )
    if not ok:
        raise RuntimeError("Chessboard not found")

    cv2.cornerSubPix(
        gray, corners, (5,5), (-1,-1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-3)
    )
    pts_img = corners.reshape(-1, 2).astype(np.float32)

    # Build metric destination for each inner corner: (i, j) * square_size
    # i in [0..cols-1], j in [0..rows-1]
    i_idx, j_idx = np.meshgrid(np.arange(cols), np.arange(rows))
    pts_world = np.column_stack([i_idx.ravel(), j_idx.ravel()]).astype(np.float32)
    # Distance between adjacent inner corners is exactly square_size_mm
    pts_dst_mm = pts_world * square_size_mm

    # Homography from image->metric plane using many correspondences
    H, inliers = cv2.findHomography(pts_img, pts_dst_mm, method=cv2.RANSAC, ransacReprojThreshold=1.5)
    if H is None:
        raise RuntimeError("findHomography failed")

    W_mm = (cols - 1) * square_size_mm
    H_mm = (rows - 1) * square_size_mm
    out_size = (int(round(W_mm * px_per_mm)), int(round(H_mm * px_per_mm)))

    # Scale metric mm to pixels by pre-multiplying with scale
    S = np.array([[px_per_mm, 0, 0],
                  [0, px_per_mm, 0],
                  [0, 0, 1]], dtype=np.float32)
    H_px = S @ H  # image -> plane-in-pixels

    rectified = cv2.warpPerspective(img_u, H_px, out_size, flags=cv2.INTER_LINEAR)
    return rectified, H, inliers


def order_quad(pts):
    """Order 4 points as TL, TR, BR, BL."""
    pts = np.array(pts, dtype=np.float32)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    tl = pts[np.argmin(s)]
    br = pts[np.argmax(s)]
    tr = pts[np.argmin(d)]
    bl = pts[np.argmax(d)]
    return np.array([tl, tr, br, bl], dtype=np.float32)



# --- Your board settings ---
pattern_size = (14, 7)      # inner corners (cols, rows) — NOT squares
square_size  = 10       # in your units (e.g., mm)

# --- Prepare one template of object points for this board ---
objp = np.zeros((pattern_size[0]*pattern_size[1], 3), np.float32)
objp[:, :2] = np.indices(pattern_size).T.reshape(-1, 2)  # (x,y) grid
objp *= square_size

# --- Collect per-image 3D/2D points ---
obj_points_3D = []  # list of (N,3) float32
img_points_2D = []  # list of (N,1,2) float32
image_size = None

criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-3)

for fn in sorted(glob.glob("img/*.jpg")):   # adjust your glob
    img = cv2.imread(fn, cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f"Skip unreadable: {fn}")
        continue

    if image_size is None:
        image_size = (img.shape[1], img.shape[0])    # (w,h)
    elif image_size != (img.shape[1], img.shape[0]):
        print(f"Skip {fn}: inconsistent image size {img.shape[::-1]}")
        continue

    ok, corners = cv2.findChessboardCorners(img, pattern_size,
                                            flags=cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE)
    if not ok:
        print(f"No corners found: {Path(fn).name}")
        continue

    # refine to sub-pixel accuracy
    corners = cv2.cornerSubPix(img, corners, (11,11), (-1,-1), criteria)

    obj_points_3D.append(objp.copy())
    img_points_2D.append(corners.astype(np.float32))

# --- Sanity checks to avoid the 'nimages > 0' assert ---
def check_inputs(obj_pts, img_pts):
    assert isinstance(obj_pts, list) and isinstance(img_pts, list), "Must be lists of per-image arrays"
    assert len(obj_pts) == len(img_pts) and len(obj_pts) > 0, f"Need >0 views; got {len(obj_pts)}"
    for i, (O, I) in enumerate(zip(obj_pts, img_pts)):
        assert O.ndim == 2 and O.shape[1] == 3, f"obj[{i}] must be (N,3)"
        N = O.shape[0]
        assert (I.shape == (N,1,2)) or (I.shape == (N,2)), f"img[{i}] shape mismatch {I.shape} vs N={N}"
        assert O.dtype in (np.float32, np.float64), "obj dtype must be float"
        assert I.dtype in (np.float32, np.float64), "img dtype must be float"

check_inputs(obj_points_3D, img_points_2D)

# --- Calibrate ---
ret, mtx, dist, rvecs, tvecs = cv2.calibrateCamera(
    obj_points_3D, img_points_2D, image_size, None, None
)
print("RMS reprojection error:", ret)
# print("K:\n", mtx)
# print("dist:", dist.ravel())

img = cv2.imread("snapshot.jpg")
h,  w = img.shape[:2]
newcameramtx, roi = cv2.getOptimalNewCameraMatrix(mtx, dist, (w,h), 1, (w,h))
undist = cv2.undistort(img, mtx, dist, None, newcameramtx)

x, y, w, h = roi
undist = undist[y:y+h, x:x+w]  # optional crop to valid pixels
rectified, _, _ = rectify_from_chessboard(undist, (14,7), 10, 10)

# 2) Detect four reference points on the plane (e.g., checkerboard outer corners)
# pts_src: 4x2 float32 in the undistorted image (clockwise order)
# Define their real-world locations on the plane in consistent units:
# e.g., a rectangle W x H in mm or “grid units”.
# W, H = 240, 135  # e.g., a 200×150 mm card
# rectified, Hmat = rectify_plane(undist, pts_src, W, H, px_per_unit=1.0)


cv2.imwrite("undistort.png", rectified)