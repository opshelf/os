import zxingcpp
import cv2
from dataclasses import dataclass
import cv2
import numpy as np
import time
from typing import Optional
import threading

@dataclass
class GS1Data:
    gtin: str
    expiry: str
    lot: str
    sn: str



class CameraDriver(threading.Thread):

    def __init__(self):
        print("Camera Initialized")
        super().__init__()
        self.daemon = True
        self._stop_event = threading.Event()
        self._cap = cv2.VideoCapture(0)
        if not self._cap.isOpened():
            raise Exception("Unable to Open Camera")
        self._frame: Optional[np.ndarray] = None
        self._crop_frames: bool = True
        self._crop_width: int = 1200
        self._crop_height: int = int(self._crop_width * (1080 / 1920))
        self._new_frame = False

    @property
    def frame(self) -> Optional[np.ndarray]:
        if self._new_frame:
            self._new_frame = False
            return self._frame
        else:
            return None

    @property
    def detected_bottle_position(self) -> Optional[np.ndarray]:
        return self._frame

    @property
    def crop_frames(self) -> bool:
        return self._crop_frames

    @crop_frames.setter
    def crop_frames(self, value: bool) -> None:
        self._crop_frames = value

    @property
    def crop_width(self) -> int:
        return self._crop_width

    @crop_width.setter
    def crop_width(self, value: int) -> None:
        self._crop_width = value

    @property
    def crop_height(self) -> int:
        return self._crop_height

    @crop_height.setter
    def crop_height(self, value: int) -> None:
        self._crop_height = value

    def run(self) -> None:
        prev_frame_time = 0
        new_frame_time = 0
        while not self._stop_event.is_set():
            font = cv2.FONT_HERSHEY_SIMPLEX
            if self._cap.isOpened():
                ret, frame = self._cap.read()
                if ret:
                    new_frame_time = time.time()
                    fps = 1 / (new_frame_time - prev_frame_time)
                    prev_frame_time = new_frame_time
                    fps = int(fps)
                    fps = str(fps)
                    cv2.putText(
                        frame,
                        fps,
                        (7, 70),
                        font,
                        3,
                        (100, 255, 0),
                        3,
                        cv2.LINE_AA,
                    )
                    barcodes = zxingcpp.read_barcodes(frame)
                    for barcode in barcodes:
                        #print(barcode)
                        print(
                            "Found barcode:"
                            f'\n Text:    "{barcode.text}"'
                            f"\n Format:   {barcode.format}"
                            f"\n Content:  {barcode.content_type}"
                            f"\n Position: {barcode.position}"
                        )
                    self._frame = frame
                    self._new_frame = True
            else:
                break

    def stop(self) -> None:
        self._stop_event.set()
        self._cap.release()

if __name__ == '__main__':
    cd = CameraDriver()
    cd.start()
    while cd.frame is None:
        pass
        time.sleep(.1)
        print("Waiting")
    while True:
        frame = cd.frame
        if frame is None:
            pass
        else:
            cv2.imshow("video", frame)
            if cv2.waitKey(1) == ord("q"):
                break
    """
    cap = cv2.VideoCapture(0)
    font = cv2.FONT_HERSHEY_SIMPLEX
    if not cap.isOpened():
        print("Cannot open camera")
        exit()
    while True and cap.isOpened():
        ret, frame = cap.read()
        if ret == True:
            new_frame_time = time.time()
            prev_frame_time = new_frame_time
            barcodes = zxingcpp.read_barcodes(frame)
            for barcode in barcodes:
                # print(barcode)
                print(
                    "Found barcode:"
                    f'\n Text:    "{barcode.text}"'
                    f"\n Format:   {barcode.format}"
                    f"\n Content:  {barcode.content_type}"
                    f"\n Position: {barcode.position}"
                )
            cv2.imshow("Frame", frame)
            if cv2.waitKey(1) == ord("q"):
                break
    cap.release()
    print("Done")
    cv2.destroyAllWindows()
    """
