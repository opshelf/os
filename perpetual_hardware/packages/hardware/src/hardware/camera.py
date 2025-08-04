import subprocess
import numpy as np
import cv2
import asyncio

#1 mm on x axis ~= 2.5 pixels 
# the camera stops working after a certain number of captures. maybe it's filling up internal memory

# todo set this with real dimensions of the fiducial
W_EXPECTED = 65
H_EXPECTED = 27
EXPECTED_AREA = W_EXPECTED * H_EXPECTED
EXPECTED_ASPECT = W_EXPECTED / H_EXPECTED
TOLERANCE_AREA = 0.1  # 50% margin
TOLERANCE_ASPECT = 0.1  # ±20%

def score(w, h):
    area_score = abs((w * h) - EXPECTED_AREA) / EXPECTED_AREA
    aspect_score = abs((w / h) - EXPECTED_ASPECT)
    return area_score + aspect_score

class Camera:
    def __init__(self, url, username='m5stack', password='12345678', timeout=10):
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout

    def _capture_sync(self):
        curl_cmd = [
            "curl",
            "-u", f"{self.username}:{self.password}",
            "--no-keepalive",
            "--silent",
            "--max-time", str(self.timeout),
            self.url
        ]
        image_bytes = subprocess.check_output(curl_cmd)
        img_array = np.asarray(bytearray(image_bytes), dtype=np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        return img

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
    
    def get_fiducial(self, frame):
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

        # print(len(contours))

        for cnt in contours:
            epsilon = 0.02 * cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, epsilon, True)

            # print(len(approx))

            if len(approx) != 4:
                # print("not box")
                continue

            x, y, w, h = cv2.boundingRect(approx)
            area = w * h
            aspect = w / h if h != 0 else 0

            if not ((1 - TOLERANCE_AREA) * EXPECTED_AREA <= area <= (1 + TOLERANCE_AREA) * EXPECTED_AREA):
                # print("bad area")
                continue
            if not ((1 - TOLERANCE_ASPECT) * EXPECTED_ASPECT <= aspect <= (1 + TOLERANCE_ASPECT) * EXPECTED_ASPECT):
                # print("bad aspect ratio")
                continue

            valid_rects.append((x, y, w, h))

        if not valid_rects:
            return None

        best = min(valid_rects, key=lambda b: score(b[2], b[3]))
        return best
