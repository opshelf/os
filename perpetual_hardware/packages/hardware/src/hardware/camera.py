import subprocess
import numpy as np
import cv2
import asyncio
import time
from typing import Optional, Tuple
from collections import defaultdict

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
class CentroidTracker:
    def __init__(self, center_thresh_px=CENTER_MATCH_PX, max_age=AGE_OUT_FRAMES):
        self.center_thresh_px = center_thresh_px
        self.max_age = max_age
        self.tracks = {}  # tid -> {"center":(x,y), "age":0}
        self.next_tid = 1

    def _match(self, centers):
        # Greedy nearest neighbor vs existing tracks
        assigned = {}
        used_tracks = set()
        for c in centers:
            best_tid, best_d2 = None, self.center_thresh_px ** 2
            for tid, t in self.tracks.items():
                if tid in used_tracks:
                    continue
                dx = c[0] - t["center"][0]
                dy = c[1] - t["center"][1]
                d2 = dx*dx + dy*dy
                if d2 <= best_d2:
                    best_tid, best_d2 = tid, d2
            if best_tid is not None:
                assigned[c] = best_tid
                used_tracks.add(best_tid)
        return assigned

    def update(self, detections):
        # Age tracks
        for tid in list(self.tracks.keys()):
            self.tracks[tid]["age"] += 1
            if self.tracks[tid]["age"] > self.max_age:
                del self.tracks[tid]

        centers = [d["center"] for d in detections]
        assigned = self._match(centers)

        new_flags = []
        tids_out = []
        for d in detections:
            c = d["center"]
            if c in assigned:
                tid = assigned[c]
                self.tracks[tid]["center"] = c
                self.tracks[tid]["age"] = 0
                tids_out.append(tid)
                new_flags.append(False)  # existing track
            else:
                tid = self.next_tid
                self.next_tid += 1
                self.tracks[tid] = {"center": c, "age": 0}
                tids_out.append(tid)
                new_flags.append(True)   # new track
        return tids_out, new_flags

class Camera:
    def __init__(self, 
                 url = "rtsp://192.168.1.108:554/user=admin&password=&channel=1&stream=0.sdp?"):
        
        self.url = url
        self.tracker = CentroidTracker()
        self.counts_new = defaultdict(int)


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
    
    def get_fiducial(self, 
                 frame,
                 draw=True):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        thresh = cv2.adaptiveThreshold(
            blurred, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=15,
            C=-5
        )

        kernel = np.ones((3, 3), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid_rects = []

        for cnt in contours:
            epsilon = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02*epsilon, True)

            if len(approx) != 4:
                continue

            x, y, w, h = cv2.boundingRect(approx)
            if (w * h < 40000) or (w * h > 80000):
                continue

            valid_rects.append((x, y, w, h))

        if not valid_rects:
            return None

        best = max(valid_rects, key=lambda b: b[2] * b[3])
        return best

    # ------------ Detection ------------
    def detect_white_squares(self, frame):
        """
        Return a list of detections:
        each detection is dict(corners=(4,2), rect=(x,y,w,h), area=float, center=(cx,cy))
        Assumes white squares on black background (or mostly dark background).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Light smoothing for speckle; keep edges crisp for quads
        gray = cv2.GaussianBlur(gray, (3, 3), 0)

        # Robust thresholding: try Otsu and invert if needed
        _, bin_ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)


        # If scene is bright / background is light, flip to ensure white squares are foreground
        # Heuristic: assume squares are smaller fraction; if foreground > 60% of pixels, invert
        # if (bin_.mean() / 255.0) > 0.60:
        #     bin_ = cv2.bitwise_not(bin_)

        # Clean small noise but keep corners
        bin_ = cv2.morphologyEx(bin_, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)

        # cv2.imwrite("bin.png", bin_)

        contours, _ = cv2.findContours(bin_, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        valid_rects = []
        detections = []
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
            # detections.append({
            #     "corners": order_corners(pts),
            #     "rect": (int(x), int(y), int(w), int(h)),
            #     "area": float(area),
            #     "center": (float(cx), float(cy)),
            # })

        if not valid_rects:
            return None

        best = max(valid_rects, key=lambda b: b[2] * b[3])
        return best        

# ------------ Main loop ------------
# def main():
#     cap = cv2.VideoCapture(SOURCE)
#     if not cap.isOpened():
#         raise RuntimeError(f"Could not open video source: {SOURCE}")

#     tracker = CentroidTracker()
#     counts_new = defaultdict(int)

#     last = time.time()
#     frames = 0
#     fps = 0.0

#     while True:
#         ok, frame = cap.read()
#         if not ok:
#             break

#         detections, bin_ = detect_white_squares(frame)
#         tids, new_flags = tracker.update(detections)


#     cap.release()
#     cv2.destroyAllWindows()

# if __name__ == "__main__":
#     main()
