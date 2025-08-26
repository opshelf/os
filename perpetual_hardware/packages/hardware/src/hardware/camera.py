import numpy as np
import cv2

# ------------ Config ------------
# SOURCE = "rtsp://admin:admin123@192.168.1.108:554/Streaming/Channels/101"
MIN_AREA = 200          # reject tiny blobs (px^2)
ASPECT_TOL = 0.5       # allow ±20% from perfect square (w≈h)
CENTER_MATCH_PX = 14    # tracker: centers closer than this = same object
AGE_OUT_FRAMES = 25     # forget objects after N frames unseen

# ------------ Geometry helpers ------------
def order_corners(pts):
    """Order four 2D points clockwise starting top-left."""
    pts = np.array(pts, dtype=np.float32).reshape(-1, 2)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).ravel()
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]  # top-left
    ordered[2] = pts[np.argmax(s)]  # bottom-right
    ordered[1] = pts[np.argmin(diff)]  # top-right
    ordered[3] = pts[np.argmax(diff)]  # bottom-left
    return ordered

def is_right_angle_quad(pts, cos_tol=0.25):
    """Check approximate right angles via vector dot-products."""
    pts = order_corners(pts)
    v = np.roll(pts, -1, axis=0) - pts
    v = v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-6)
    # Adjacent edges dot products ~ 0 for right angles
    dots = [abs(np.dot(v[i], v[(i+1) % 4])) for i in range(4)]
    return all(d <= cos_tol for d in dots)

# ------------ Simple centroid tracker (no IDs on squares) ------------

class Camera:
    def __init__(self, 
                 url = "rtsp://192.168.1.108:554/user=admin&password=&channel=1&stream=0.sdp?tcp"):
        self.url = url

    def get_center(self, frame, object_bbox):
        frame_h, frame_w = frame.shape[:2]
        frame_center = (frame_w / 2, frame_h / 2)

        x, y, w, h = object_bbox
        object_center = (x + w / 2, y + h / 2)

        dist = np.linalg.norm(np.array(frame_center) - np.array(object_center))
        max_dist = np.linalg.norm(np.array([0, 0]) - np.array(frame_center))
        centered_score = dist / max_dist

        cx = frame_center[0] - object_center[0] 
        cy = frame_center[1] - object_center[1]

        return cx, cy

    def detect_white_squares(self, frame):
        """
        Return a list of detections:
        each detection is dict(corners=(4,2), rect=(x,y,w,h), area=float, center=(cx,cy))
        Assumes white squares on black background (or mostly dark background).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, bin_ = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
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