import cv2
import threading
from typing import Optional
import zxingcpp
import cv2
from pydantic import BaseModel, ConfigDict
import queue
import re
from perpetual_p0c.storage import GS1Data


class RawBarCode(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    text: str
    format: zxingcpp.BarcodeFormat
    content: zxingcpp.ContentType


def parse_gs1_data(text: str) -> GS1Data:
    patterns = {
        "gtin": r"\(01\)(\d{14})",
        "sn": r"\(21\)([^\(]+)",
        "expiry": r"\(17\)(\d{6})",
        "lot": r"\(10\)([^\(]+)",
    }

    # Extract data using regex
    data = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            data[key] = match.group(1)
        else:
            data[key] = None

    # Create the GS1Data model
    return GS1Data(gtin=data["gtin"], lot=data["lot"], expiry=data["expiry"], sn=data["sn"])


def parse_barcode(rbc: RawBarCode) -> Optional[GS1Data]:
    # if rbc.format == zxingcpp.BarcodeFormat.UPCA:
    #    return UPCData(text=rbc.text)
    if rbc.format == zxingcpp.BarcodeFormat.DataMatrix:
        return parse_gs1_data(rbc.text)
    else:
        # raise Exception("Unexpected Format")
        return None


class Camera:
    """
    A class to handle video capture from a camera.
    """

    def __init__(self, event_queue: queue.Queue) -> None:
        """
        Initialize the camera.

        :param camera_index: Index of the camera to use.
        """
        self.cap = cv2.VideoCapture(0)
        self.lock = threading.Lock()
        self.event_queue = event_queue

    def get_frame(self) -> bytes:
        """
        Capture a frame from the camera.

        :return: JPEG encoded image bytes.
        """
        with self.lock:
            ret, frame = self.cap.read()
            if not ret:
                return b""

            barcodes = zxingcpp.read_barcodes(frame)
            for barcode in barcodes:
                try:
                    # print(
                    #    "Found barcode:"
                    #    f'\n Text:    "{barcode.text}"'
                    #    f"\n Format:   {barcode.format}"
                    #    f"\n Content:  {barcode.content_type}"
                    #    f"\n Position: {barcode.position}"
                    # )
                    bc = parse_barcode(
                        RawBarCode(
                            text=barcode.text, format=barcode.format, content=barcode.content_type
                        )
                    )
                    if bc:
                        self.event_queue.put(bc)
                except Exception as e:
                    print(e)

            ret, jpeg = cv2.imencode(".jpg", frame)

            if not ret:
                return b""

            return jpeg.tobytes()

    def release(self) -> None:
        with self.lock:
            if self.cap.isOpened():
                self.cap.release()
