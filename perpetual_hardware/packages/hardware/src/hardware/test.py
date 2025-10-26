"""
Quick manual test for the Arducam ToF camera connected over the CSI interface.

The script attempts to open sensor IDs 0/1/2 (Jetson CSI ports) via a GStreamer
pipeline, captures a single frame, and saves it to disk for inspection.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import sleep

import cv2


def build_gstreamer_pipeline(sensor_id: int, width: int, height: int, fps: int) -> str:
    """
    Compose a GStreamer pipeline that uses nvarguscamerasrc to talk to a CSI camera.
    """
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} ! "
        f"video/x-raw(memory:NVMM), width={width}, height={height}, framerate={fps}/1 ! "
        "nvvidconv flip-method=0 ! video/x-raw, format=BGRx ! "
        "videoconvert ! video/x-raw, format=BGR ! appsink drop=true sync=false"
    )


def capture_frame(sensor_id: int, output_path: Path, width: int, height: int, fps: int) -> None:
    """
    Open the camera on the requested CSI port and persist a single frame to disk.
    """
    pipeline = build_gstreamer_pipeline(sensor_id, width, height, fps)
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

    if not cap.isOpened():
        raise RuntimeError(
            f"Unable to open Arducam ToF on CSI sensor-id {sensor_id}. "
            "Ensure the camera is connected and the nvargus daemon is running."
        )

    # Allow the auto-exposure/auto-gain to settle before grabbing the frame.
    sleep(0.5)
    for _ in range(3):
        ret, frame = cap.read()
        if ret:
            break
        sleep(0.1)
    else:
        cap.release()
        raise RuntimeError("Failed to read a frame from the camera.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_path), frame):
        cap.release()
        raise RuntimeError(f"cv2.imwrite failed for path: {output_path}")

    cap.release()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Take a snapshot with the Arducam ToF camera.")
    parser.add_argument(
        "--port",
        type=int,
        choices=(0, 1, 2),
        default=0,
        help="CSI port / sensor-id to open (default: 0).",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1280,
        help="Capture width (default: 1280).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=720,
        help="Capture height (default: 720).",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Desired frame rate (default: 30).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("arducam_tof_snapshot.jpg"),
        help="Destination image path (default: ./arducam_tof_snapshot.jpg).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        capture_frame(args.port, args.output, args.width, args.height, args.fps)
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)

    print(f"Saved snapshot from sensor-id {args.port} to {args.output.resolve()}")


if __name__ == "__main__":
    main()
