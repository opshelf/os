#!/usr/bin/env python3
import os, sys, time, cv2

def grab_snapshot(rtsp_url: str, out_path: str = "snapshot.jpg", warmup_frames: int = 8) -> None:
    # Make ffmpeg-based RTSP more reliable (TCP + 5s timeout).
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")

    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)  # or cv2.CAP_GSTREAMER if you prefer
    if not cap.isOpened():
        raise RuntimeError("Failed to open RTSP stream. Check IP, creds, and that the stream path is correct.")

    # Reduce latency
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    # Warm up decoder (helpful for H.264 keyframes)
    for _ in range(warmup_frames):
        ok, _ = cap.read()
        if not ok:
            time.sleep(0.05)

    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError("Could not read a frame from the stream.")

    if not cv2.imwrite(out_path, frame):
        raise RuntimeError(f"Failed to write image to {out_path}")

    print(f"Saved snapshot to {out_path}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python grab_snapshot.py rtsp://user:pass@CAM_IP:554/stream1 [output.jpg]")
        sys.exit(1)
    url = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else "snapshot.jpg"
    grab_snapshot(url, out)
