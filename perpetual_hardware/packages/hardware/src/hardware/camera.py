import numpy as np
import cv2
import time
from pathlib import Path

MIN_AREA = 200          # reject tiny blobs (px^2)
ASPECT_TOL = 0.75       # allow ±20% from perfect square (w≈h)

def try_find_board(gray, sizes):
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



def _rectify_from_chessboard(img_u, pattern_size=(14,7), square_size_mm=10, px_per_mm=10):
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
    return rectified, H, inliers

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


class Camera:
    def __init__(self, 
                 url="rtsp://192.168.1.108:554/user=admin&password=&channel=1&stream=0.sdp?tcp"):
        self.url = url
        self.mtx = None
        self.dist = None
        self.rvecs = None
        self.tvecs = None
        self.newcameramtx = None
        self.roi = None
        self.H = None

        # remap fields
        self.map1 = None
        self.map2 = None
        self.map_size = None  # (w,h) used to build maps

        self.H_ref_size = (2880, 1620)  # (w0, h0)
        self.board_W_mm = None
        self.board_H_mm = None
        self.pattern_size = (14, 7)
        self.square_size_mm = 10.0

        # self.get_correct()

    async def capture(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise RuntimeError("bad cam")
        
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 5000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 5000)

        t0 = time.time()
        while time.time() - t0 < 4.25: # idr interval
            cap.grab()
            time.sleep(0.001)

        for _ in range(5):
            cap.grab()

        ok, frame = cap.read()
        if not ok or frame is None:
            print("camera fail")
            return None
        
        cap.release()
        return frame
    
    def _build_remap(self, size_wh):
        """(Re)compute remap for given (w,h) using current intrinsics."""
        w, h = size_wh
        self.newcameramtx, self.roi = cv2.getOptimalNewCameraMatrix(
            self.mtx, self.dist, (w, h), alpha=1, newImgSize=(w, h)
        )
        # Identity rectification (no stereo R), just undistort into newcameramtx
        R = np.eye(3, dtype=np.float32)
        self.map1, self.map2 = cv2.initUndistortRectifyMap(
            self.mtx, self.dist, R, self.newcameramtx, (w, h), cv2.CV_32FC1
        )
        self.map_size = (w, h)

    def get_correct(self):
        # --- Calibrate intrinsics
        obj3d, img2d, img_sz = get_undistortion(self.pattern_size, self.square_size_mm)
        ret, self.mtx, self.dist, self.rvecs, self.tvecs = cv2.calibrateCamera(
            obj3d, img2d, img_sz, None, None
        )

        # --- Build remap for a known still image and compute H on that same size
        base = Path(__file__).resolve().parent if '__file__' in globals() else Path.cwd()
        img_path = base / "img" / "1.jpg"     # image used to compute H
        img = cv2.imread(str(img_path))
        if img is None:
            raise FileNotFoundError(f"Calibration image not found: {img_path}")
        h0, w0 = img.shape[:2]
        self._build_remap((w0, h0))
        undist0 = cv2.remap(img, self.map1, self.map2, interpolation=cv2.INTER_LINEAR)

        # Compute H on the undistorted full image (no ROI crop)
        rectified0, self.H, _, detected_ps = rectify_from_chessboard(
            undist0, pattern_size=None, square_size_mm=self.square_size_mm, px_per_mm=10
        )
        self.pattern_size = detected_ps  # remember what we actually have

        cols, rows = self.pattern_size
        self.board_W_mm = (cols - 1) * self.square_size_mm
        self.board_H_mm = (rows - 1) * self.square_size_mm

        # cv2.imwrite("rectified.jpg", rectified0)

    def correct(self, img):
        """Undistort with remap, then rectify with H scaled to the current size."""
        h, w = img.shape[:2]
        if self.map1 is None or self.map_size != (w, h):
            self._build_remap((w, h))

        undist = cv2.remap(img, self.map1, self.map2, interpolation=cv2.INTER_LINEAR)

        # print("undist")

        # Scale H if current size != reference size used to compute H
        w0, h0 = self.H_ref_size
        if (w, h) != (w0, h0):
            S_in = np.array([[w0 / w, 0, 0],
                             [0, h0 / h, 0],
                             [0,     0, 1]], dtype=np.float32)
            H_use = self.H @ S_in
        else:
            H_use = self.H

        # (Optional) If you insist on cropping by ROI before H, compensate with a translation:
        # x,y,ww,hh = self.roi
        # T = np.array([[1,0,-x],[0,1,-y],[0,0,1]], np.float32)
        # H_use = H_use @ T
        # undist = undist[y:y+hh, x:x+ww]

        # Rectify to the *board* size to avoid wild extrapolation
        rectified = rectify_with_H(
            undist, H_use,
            plane_W_mm=self.board_W_mm,
            plane_H_mm=self.board_H_mm,
            px_per_mm=10
        )
        # print("saving")
        # cv2.imwrite("rectified.jpg", rectified)

        return rectified



    def get_center(self, frame, object_bbox):
        frame_h, frame_w = frame.shape[:2]
        frame_center = (frame_w / 2, frame_h / 2)

        x, y, w, h = object_bbox
        object_center = (x + w / 2, y + h / 2)

        cx = frame_center[0] - object_center[0] 
        cy = frame_center[1] - object_center[1]

        return cx, cy

    def detect_white_squares(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, bin_ = cv2.threshold(gray, 225, 255, cv2.THRESH_BINARY) # 200
        bin_ = cv2.morphologyEx(bin_, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
        cv2.imwrite("bin.png", bin_)
        contours, _ = cv2.findContours(bin_, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        valid_rects = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < MIN_AREA:
                continue
            # Polygonal approximation
            eps = 0.02 * cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, eps, True)
            if len(approx) != 4:
                continue
            if not cv2.isContourConvex(approx):
                continue
            pts = approx.reshape(-1, 2).astype(np.float32)
            if not is_right_angle_quad(pts):
                continue

            x, y, w, h = cv2.boundingRect(approx)
            if w == 0 or h == 0:
                continue
            aspect = w / float(h)
            if not (1.0 - ASPECT_TOL <= aspect <= 1.0 + ASPECT_TOL):
                continue

            cx, cy = (x + w / 2.0, y + h / 2.0)
            valid_rects.append((x, y, w, h))

        if not valid_rects:
            return None

        best = max(valid_rects, key=lambda b: b[2] * b[3])
        return best        