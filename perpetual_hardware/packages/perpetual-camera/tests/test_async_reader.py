from perpetual_camera.async_reader import AsyncCameraReader
import asyncio
import cv2
import numpy as np
import threading

async def main() -> None:
    reader = AsyncCameraReader(0)

    def show_frame(frame: cv2.typing.MatLike) -> None: 
        print(frame)
        cv2.imshow("Camera Feed", frame)  # Display the frame

    reader.add_callback(show_frame)
    started = False
    while True:
        if not started:
            reader.start()
        # Press 'q' to exit
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    reader.stop()
    cv2.destroyAllWindows() 

if __name__ == '__main__':
    # asyncio.run(main())
    # Example callback functions

    main_frame = None

    def display_frame(frame: cv2.typing.MatLike) -> None:
        global main_frame
        main_frame = frame

    # Create and use the camera reader
    camera = AsyncCameraReader(camera_id=0, fps_limit=30)
    camera.add_callback(display_frame)
    camera.start()
    while True:
        if main_frame is not None:
            cv2.imshow("Camera Feed", main_frame)  # Display the frame
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            main_frame = None
    camera.stop()
    cv2.destroyAllWindows()

