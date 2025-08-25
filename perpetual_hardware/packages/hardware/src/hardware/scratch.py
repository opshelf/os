from pymodbus.client import ModbusSerialClient as ModbusClient
# import time
# import cv2
# slave_id = 9
# import numpy as np

# import cv2

import subprocess
import numpy as np
import cv2
import asyncio
import time
from typing import Optional, Tuple

# class Camera:
#     def __init__(self, 
#                  exp_area,
#                  exp_asp,
#                  url = "http://192.168.1.108/CGI/command/snap?channel=0", 
#                  username='admin', 
#                  password='admin123', 
#                  timeout=10):
        
#         self.url = url
#         self.username = username
#         self.password = password
#         self.timeout = timeout
#         self.ea = exp_area
#         self.ep = exp_asp

#     def _capture_sync(self, timeout=5):
#         curl_cmd = [
#             "curl",
#             "-u", f"{self.username}:{self.password}",
#             "--silent",
#             "--max-time", str(self.timeout),
#             self.url
#         ]
#         try:
#             proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE)
#             image_bytes, _ = proc.communicate(timeout=timeout)
#             img_array = np.asarray(bytearray(image_bytes), dtype=np.uint8)
#             img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
#             img = cv2.resize(img, None, fx=1.4, fy=1, interpolation=cv2.INTER_LINEAR)
#             return img
#         except subprocess.TimeoutExpired:
#             proc.kill()
#             print("Camera curl timed out")
#             return None
#         except Exception as e:
#             print("Camera capture failed:", e)
#         return None

#     async def capture(self, save_path=None):
#         img = await asyncio.to_thread(self._capture_sync)
#         if save_path:
#             cv2.imwrite(save_path, img)
#         return img
    
#     def capture_sync(self, save_path=None):
#         img = self._capture_sync()
#         if save_path:
#             cv2.imwrite(save_path, img)
#         return img
    
from collections import defaultdict

# ------------ Config ------------
SOURCE = "rtsp://192.168.1.108:554/user=admin&password=&channel=1&stream=0.sdp?"
MIN_AREA = 200          # reject tiny blobs (px^2)
ASPECT_TOL = 0.50       # allow ±20% from perfect square (w≈h)
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

#todo swap this in for get_center
# ------------ Detection ------------
def detect_white_squares(frame):
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
    if (bin_.mean() / 255.0) > 0.60:
        bin_ = cv2.bitwise_not(bin_)

    # Clean small noise but keep corners
    bin_ = cv2.morphologyEx(bin_, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(bin_, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

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
        detections.append({
            "corners": order_corners(pts),
            "rect": (int(x), int(y), int(w), int(h)),
            "area": float(area),
            "center": (float(cx), float(cy)),
        })
    return detections, bin_

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

# ------------ Main loop ------------
def main():
    cap = cv2.VideoCapture(SOURCE)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video source: {SOURCE}")

    tracker = CentroidTracker()
    counts_new = defaultdict(int)

    last = time.time()
    frames = 0
    fps = 0.0

    for i in range(0,10):
        ok, frame = cap.read()
        if not ok:
            break

        frame = cv2.resize(frame, None, fx=1.1, fy=1, interpolation=cv2.INTER_LINEAR)
        cv2.imwrite("beb.jpg", frame)

        detections, bin_ = detect_white_squares(frame)

        print(detections[0].get("center"))

        # tids, new_flags = tracker.update(detections)

        # # Draw bin preview (small)
        # small = cv2.resize(bin_, (0, 0), fx=0.3, fy=0.3, interpolation=cv2.INTER_NEAREST)
        # frame[0:small.shape[0], 0:small.shape[1]] = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)

        # # Draw detections
        # for det, tid, is_new in zip(detections, tids, new_flags):
        #     corners = det["corners"].astype(int)
        #     x, y, w, h = det["rect"]
        #     cx, cy = map(int, det["center"])
        #     color = (0, 255, 0) if is_new else (0, 180, 255)
        #     # box
        #     cv2.polylines(frame, [corners], True, color, 2, cv2.LINE_AA)
        #     # center
        #     cv2.circle(frame, (cx, cy), 3, color, -1)
        #     # label
        #     msg = f"T{tid}" + (" NEW" if is_new else "")
        #     cv2.putText(frame, msg, (x, y - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        #     if is_new:
        #         counts_new[tid] += 1

    #     # FPS display
    #     frames += 1
    #     now = time.time()
    #     if now - last >= 0.5:
    #         fps = frames / (now - last)
    #         frames = 0
    #         last = now
    #     cv2.putText(frame, f"FPS {fps:.1f}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 2, cv2.LINE_AA)

    #     # Count summary
    #     y0 = 45
    #     for i, (tid, cnt) in enumerate(sorted(counts_new.items())):
    #         cv2.putText(frame, f"T{tid}: {cnt}", (10, y0 + 20*i),
    #                     cv2.FONT_HERSHEY_SIMPLEX, 0.55, (220,255,220), 1, cv2.LINE_AA)

    #     cv2.imshow("White-Square Fiducials", frame)
    #     key = cv2.waitKey(1) & 0xFF
    #     if key in (27, ord('q')):
    #         break

    cap.release()
    # cv2.destroyAllWindows()

if __name__ == "__main__":
    main()


# client = ModbusClient(
#     port="/dev/ttyUSB0",
#     baudrate=115200,
#     timeout=1,
#     stopbits=1,
#     bytesize=8,
#     parity='N'  # Use 'E' or 'O' if your device uses even/odd parity
# )

# if client.connect():
#     print("connected")
#     result = client.read_holding_registers(0x138E, count=1, slave=slave_id)
#     print(int(result.registers[0]))
#     client.write_register(0x03E8, 0x0, slave=slave_id)
#     client.write_register(0x138E, 0x3, slave=slave_id)
#     client.write_register(0x03E8, 0x1, slave=slave_id)
#     result = client.read_holding_registers(0x138E, count=1, slave=slave_id)
#     print(int(result.registers[0]))

    # client.write_register(0x3, 0x2, slave=slave_id)
    # client.write_register(0x1801, 0x1111, slave=slave_id) # clear errors
    # client.write_register(0x1801, 0x1122, slave=slave_id) # clear errors
    # client.write_register(0x00B, 4000, slave=slave_id)
    # client.write_register(0x0159, 0xA1, slave=slave_id)
    # client.write_register(0x0167, 250, slave=slave_id)
    # client.write_register(0x0169, 250, slave=slave_id)
    # client.write_register(0x016B, 10, slave=slave_id)
    # client.write_register(0x0007, 0, slave=slave_id) 
    # client.write_register(0x6200, 0x41, slave=slave_id)
    # client.write_register(0x0191, 0x24, slave=slave_id)
    # client.write_register(0x6203, 0xFF, slave=slave_id)
    # client.write_register(0x6204, 0x32, slave=slave_id)
    # client.write_register(0x6205, 0x32, slave=slave_id)
    # client.write_register(0x6202, 1000, slave=slave_id)
    # client.write_register(0x6002, 0x10, slave=slave_id)

