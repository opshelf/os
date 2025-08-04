import subprocess
import numpy as np
import cv2
import asyncio

class Camera:
    def __init__(self, url, username='m5stack', password='12345678', timeout=5):
        self.url = url
        self.username = username
        self.password = password
        self.timeout = timeout

    def _capture_sync(self):
        curl_cmd = [
            "curl",
            "-u", f"{self.username}:{self.password}",
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
    
    def get_center(frame, object_bbox):
        frame_h, frame_w = frame.shape[:2]
        frame_center = (frame_w / 2, frame_h / 2)

        x, y, w, h = object_bbox
        object_center = (x + w / 2, y + h / 2)

        dist = np.linalg.norm(np.array(frame_center) - np.array(object_center))
        max_dist = np.linalg.norm(np.array([0, 0]) - np.array(frame_center))
        centered_score = dist / max_dist

        return centered_score, object_center, frame_center
    
    def get_fiducial(frame):
         # Convert to grayscale
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Blur to reduce noise
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)

        # Adaptive threshold to isolate white regions
        thresh = cv2.adaptiveThreshold(
            blurred, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=15,
            C=-5
        )

        # Morphological cleanup
        kernel = np.ones((3, 3), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

        # Find contours
        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        rectangles = []
        for cnt in contours:
            epsilon = 0.02 * cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, epsilon, True)
            if len(approx) == 4 and cv2.contourArea(approx) > 500:
                rectangles.append(approx)

        if not rectangles:
            return None

        largest_rect = max(rectangles, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(largest_rect)
        return (x, y, w, h)

# Example usage
# async def main():
#     cam = PoECamera("http://10.254.239.142/") # have this load from the ifconfig of the pi
#     img = await cam.capture("snapshot.jpg")
#     print("Image saved and returned.")

# if __name__ == "__main__":
#     asyncio.run(main())
