import cv2

def capture_usb_image(device_index=0, output_path="usb_image.jpg"):
    # Open the USB camera (e.g., /dev/video0 -> index 0)
    cap = cv2.VideoCapture(device_index)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open USB camera at index {device_index}")

    # Read one frame
    ret, frame = cap.read()
    if not ret:
        raise RuntimeError("Failed to capture image from USB camera")

    # Save the image
    cv2.imwrite(output_path, frame)
    print(f"Saved image to {output_path}")

    # Release resources
    cap.release()

if __name__ == "__main__":
    capture_usb_image(device_index=0, output_path="usb_image.jpg")
