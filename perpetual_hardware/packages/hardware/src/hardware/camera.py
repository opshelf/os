import subprocess
import numpy as np
import cv2
import asyncio

class Camera:
    def __init__(self, 
                 exp_area,
                 exp_asp,
                 url = "http://192.168.1.108/CGI/command/snap?channel=0", 
                 username='admin', 
                 password='admin123', 
                 timeout=10):
        
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout
        self.ea = exp_area
        self.ep = exp_asp

    def _capture_sync(self, timeout=5):
        curl_cmd = [
            "curl",
            "-u", f"{self.username}:{self.password}",
            "--silent",
            "--max-time", str(self.timeout),
            self.url
        ]
        try:
            proc = subprocess.Popen(curl_cmd, stdout=subprocess.PIPE)
            image_bytes, _ = proc.communicate(timeout=timeout)
            img_array = np.asarray(bytearray(image_bytes), dtype=np.uint8)
            img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            return img
        except subprocess.TimeoutExpired:
            proc.kill()
            print("Camera curl timed out")
            return None
        except Exception as e:
            print("Camera capture failed:", e)
        return None

    async def capture(self, save_path=None):
        img = await asyncio.to_thread(self._capture_sync)
        if save_path:
            cv2.imwrite(save_path, img)
        return img
    
    def capture_sync(self, save_path=None):
        img = self._capture_sync()
        if save_path:
            cv2.imwrite(save_path, img)
        return img
    
    def get_center(self, frame, object_bbox):
        frame_h, frame_w = frame.shape[:2]
        frame_center = (frame_w / 2, frame_h / 2)

        x, y, w, h = object_bbox
        object_center = (x + w / 2, y + h / 2)

        dist = np.linalg.norm(np.array(frame_center) - np.array(object_center))
        max_dist = np.linalg.norm(np.array([0, 0]) - np.array(frame_center))
        centered_score = dist / max_dist

        return centered_score, object_center, frame_center
    
    def get_fiducial(self, 
                 frame,
                 EXPECTED_AREA,
                 EXPECTED_ASPECT,
                 TOLERANCE_AREA,
                 TOLERANCE_ASPECT,
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
            if (w * h < 1000) or (w * h > 2000):
                continue

            valid_rects.append((x, y, w, h))

        if not valid_rects:
            return None

        best = max(valid_rects, key=lambda b: b[2] * b[3])
        return best
    
    # def score(self, w, h):
    #     area_score = abs((w * h) - self.ea) / self.ea
    #     aspect_score = abs((w / h) - self.ep)
    #     return area_score + aspect_score


    # def list_rectangular_contours(self, frame, draw=True, min_area=50):
        """
        Finds all rectangular contours and returns a list of (x, y, w, h).
        If draw=True, draws each rectangle and labels it with WxH.
        """
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

        rects = []
        for cnt in contours:
            # polygonal approximation
            peri = cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)

            # keep 4-vertex convex shapes
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue

            x, y, w, h = cv2.boundingRect(approx)
            if w * h < min_area:
                continue

            rects.append((x, y, w, h))

        # sort by area (largest first) for a stable order
        rects.sort(key=lambda b: b[2] * b[3], reverse=True)

        if draw:
            for (x, y, w, h) in rects:
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 255), 2)
                cv2.putText(frame, f"{w}x{h}", (x, y - 6),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)

        # Optional: print the list to console
        for i, (x, y, w, h) in enumerate(rects):
            print(f"[{i}] width (x-dim) = {w}, height (y-dim) = {h}, at (x={x}, y={y})")

        cv2.imwrite("beb.png",frame)

        return rects