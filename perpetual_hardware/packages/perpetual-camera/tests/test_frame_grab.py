import cv2

if __name__ == '__main__':
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Error: Could not open camera.")
        exit()
    while True:
        ret, frame = cap.read()  # Read a frame from the camera
        if not ret:
            print("Error: Failed to capture frame.")
            break
        cv2.imshow("Camera Feed", frame)  # Display the frame
        # Press 'q' to exit
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    # Release resources
    cap.release()
    cv2.destroyAllWindows() 
