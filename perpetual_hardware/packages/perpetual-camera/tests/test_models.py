import cv2
import cv2
import numpy as np
from typing import Tuple
import cv2
from skimage.metrics import structural_similarity

def save_baseline_image(frame: np.ndarray, path: str = "baseline.jpg") -> None:
    cv2.imwrite(path, frame)

def get_baseline_image(path: str = "baseline.jpg") -> np.ndarray:
    return cv2.imread(path)

def prepare_image(frame: np.ndarray) -> np.ndarray:
    blurred_image = cv2.GaussianBlur(frame, (5, 5), 0) 
    gray_image = cv2.cvtColor(blurred_image, cv2.COLOR_BGR2GRAY)
    _, thresholded_image = cv2.threshold(gray_image, 127, 255, cv2.THRESH_BINARY)
    #kernel = np.ones((12, 12), np.uint8)
    #closing = cv2.morphologyEx(frame, cv2.MORPH_CLOSE, kernel)
    #img = cv2.cvtColor(closing, cv2.COLOR_BGR2GRAY)
    #return thresholded_image
    return thresholded_image

def item_present(baseline: np.ndarray, frame: np.ndarray) -> Tuple[bool, float, np.ndarray]:
    cv2.imshow("Diff", frame)
    baseline = get_baseline_image()
    prepared_frame = prepare_image(frame)
    prepared_baseline = prepare_image(baseline)
    score, _ = structural_similarity(prepared_baseline, prepared_frame, full=True)
    print("Similarity Score: {:.3f}%".format(score * 100))
    diff = 255 - cv2.absdiff(prepared_baseline, prepared_frame)
    kernel = np.ones((12, 12), np.uint8)
    closing = cv2.morphologyEx(diff, cv2.MORPH_CLOSE, kernel)
    #img = cv2.cvtColor(closing, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(closing, 100, 255, cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    MIN_CONTOUR_AREA = 10000    # Adjust based on your use case
    MAX_CONTOUR_AREA = 500000
    filtered_contours = [cnt for cnt in contours if MIN_CONTOUR_AREA < cv2.contourArea(cnt) < MAX_CONTOUR_AREA]
    cv2.drawContours(frame, filtered_contours, -1, (0,255,0), 3)
    is_present = len(filtered_contours) > 0 or (score * 100) < 85.0
    return (is_present, score, frame)


def test_capture() -> None:
    baseline = get_baseline_image()
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: Could not open camera.")
        exit()
    while True:
        ret, frame = cap.read()  # Read a frame from the camera
        if not ret:
            print("Error: Failed to capture frame.")
            break
        prepared_frame = prepare_image(frame)
        prepared_baseline = prepare_image(baseline)
        score, _ = structural_similarity(prepared_baseline, prepared_frame, full=True)
        print("Similarity Score: {:.3f}%".format(score * 100))
        diff = 255 - cv2.absdiff(prepared_baseline, prepared_frame)
        kernel = np.ones((12, 12), np.uint8)
        closing = cv2.morphologyEx(diff, cv2.MORPH_CLOSE, kernel)
        ret, binary = cv2.threshold(closing, 100, 255, cv2.THRESH_OTSU) #type: ignore
        contours, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        MIN_CONTOUR_AREA = 10000    # Adjust based on your use case
        MAX_CONTOUR_AREA = 500000
        filtered_contours = [cnt for cnt in contours if MIN_CONTOUR_AREA < cv2.contourArea(cnt) < MAX_CONTOUR_AREA]
        cv2.drawContours(frame, filtered_contours, -1, (0,255,0), 3)

        cv2.imshow("Diff", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows() 

def test_presence() -> None:
    baseline = get_baseline_image()
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: Could not open camera.")
        exit()
    while True:
        ret, frame = cap.read()  # Read a frame from the camera
        if not ret:
            print("Error: Failed to capture frame.")
            break
        present, similarity, contoured_frame = item_present(baseline, frame)
        print(present, similarity)
        cv2.imshow("Diff", contoured_frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    cap.release()
    cv2.destroyAllWindows() 

if __name__ == '__main__':
    test_presence()

