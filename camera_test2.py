import cv2

img = cv2.imread("snapshot.jpg")
img = cv2.rotate(img, cv2.ROTATE_180)
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
_, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)

contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

for cnt in contours:
    x, y, w, h = cv2.boundingRect(cnt)
    area = w * h
    print(f"Found object: width={w}px, height={h}px, area={area}px²")
    cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 0), 2)