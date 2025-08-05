import cv2
import numpy as np

def detect_white_rectangle(frame):
    """
    Detect the largest white rectangular contour in the foreground.

    Args:
        frame (numpy.ndarray): Input image/frame in BGR.

    Returns:
        tuple: Bounding box (x, y, w, h) of detected rectangle or None if not found.
    """
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

def get_object_centered_score(frame, object_bbox):
    frame_h, frame_w = frame.shape[:2]
    frame_center = (frame_w / 2, frame_h / 2)

    x, y, w, h = object_bbox
    object_center = (x + w / 2, y + h / 2)

    dist = np.linalg.norm(np.array(frame_center) - np.array(object_center))
    max_dist = np.linalg.norm(np.array([0, 0]) - np.array(frame_center))
    centered_score = dist / max_dist

    return centered_score, object_center, frame_center

if __name__ == "__main__":
    # Load and rotate image 180 degrees
    frame = cv2.imread("snapshot.jpg")
    frame = cv2.rotate(frame, cv2.ROTATE_180)

    bbox = detect_white_rectangle(frame)

    if bbox is None:
        print("No white rectangular fiducial detected.")
    else:
        score, obj_center, frame_center = get_object_centered_score(frame, bbox)
        print(f"Centered score (0 = perfect): {score:.3f}")
        print(f"Object center: {obj_center}")
        print(f"Frame center: {frame_center}")
        

        # Draw bounding box and centers
        x, y, w, h = bbox
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
        print(f"Found object: width={w}px, height={h}px, area={w*h}px²")
        cv2.circle(frame, (int(obj_center[0]), int(obj_center[1])), 5, (255, 0, 0), -1)
        cv2.circle(frame, (int(frame_center[0]), int(frame_center[1])), 5, (0, 0, 255), -1)

    # Save final result
    cv2.imwrite("output_fiducial_detected.jpg", frame)

    # Optional: save thresholding steps (for debugging)
    # gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    # blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    # thresh = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
    #                                cv2.THRESH_BINARY, 15, -5)
    # kernel = np.ones((3, 3), np.uint8)
    # cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)

    # cv2.imwrite("step_thresh.jpg", thresh)
    # cv2.imwrite("step_cleaned.jpg", cleaned)

    # Show result
    # cv2.imshow("Fiducial Detection", frame)
    # cv2.waitKey(0)
    # cv2.destroyAllWindows()
