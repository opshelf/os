import numpy as np
import cv2
import time
from pathlib import Path
from hardware import toolcam_helpers

MIN_AREA = 200          # reject tiny blobs (px^2)
ASPECT_TOL = 0.75       # allow ±20% from perfect square (w≈h)

# todo cache all these parameters
class ToolCamera:
    def __init__(self, params: dict):
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
        obj3d, img2d, img_sz = toolcam_helpers.get_undistortion(self.pattern_size, self.square_size_mm)
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
        rectified0, self.H, _, detected_ps = toolcam_helpers.rectify_from_chessboard(
            undist0, pattern_size=None, square_size_mm=self.square_size_mm, px_per_mm=10
        )
        self.pattern_size = detected_ps  # remember what we actually have

        cols, rows = self.pattern_size
        self.board_W_mm = (cols - 1) * self.square_size_mm
        self.board_H_mm = (rows - 1) * self.square_size_mm


    def correct(self, img):
        """Undistort with remap, then rectify with H scaled to the current size."""
        h, w = img.shape[:2]
        if self.map1 is None or self.map_size != (w, h):
            self._build_remap((w, h))

        undist = cv2.remap(img, self.map1, self.map2, interpolation=cv2.INTER_LINEAR)

        # Scale H if current size != reference size used to compute H
        w0, h0 = self.H_ref_size
        if (w, h) != (w0, h0):
            S_in = np.array([[w0 / w, 0, 0],
                             [0, h0 / h, 0],
                             [0,     0, 1]], dtype=np.float32)
            H_use = self.H @ S_in
        else:
            H_use = self.H

        # Rectify to the *board* size to avoid wild extrapolation
        rectified = toolcam_helpers.rectify_with_H(
            undist, H_use,
            plane_W_mm=self.board_W_mm,
            plane_H_mm=self.board_H_mm,
            px_per_mm=10
        )

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
        _, bin_ = cv2.threshold(gray, 225, 255, cv2.THRESH_BINARY) # 200 #TODO param
        bin_ = cv2.morphologyEx(bin_, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)
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
            if not toolcam_helpers.is_right_angle_quad(pts):
                continue

            x, y, w, h = cv2.boundingRect(approx)
            if w == 0 or h == 0:
                continue
            aspect = w / float(h)
            if not (1.0 - ASPECT_TOL <= aspect <= 1.0 + ASPECT_TOL): #TODO param
                continue

            cx, cy = (x + w / 2.0, y + h / 2.0)
            valid_rects.append((x, y, w, h))

        if not valid_rects:
            return None

        best = max(valid_rects, key=lambda b: b[2] * b[3])
        return best        