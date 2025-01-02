import cv2
from typing import Optional, Union, List
import zxingcpp
import cv2
from .base import Scanner, ScannerType, BaseBarCode, BarCodeWithImage
import asyncio

# class RawBarCode(BaseModel):
#    model_config = ConfigDict(arbitrary_types_allowed=True)
#    text: str
#    format: zxingcpp.BarcodeFormat
#    content: zxingcpp.ContentType
# def parse_gs1_data(text: str) -> GS1Data:
#    patterns = {
#        "gtin": r"\(01\)(\d{14})",
#        "sn": r"\(21\)([^\(]+)",
#        "expiry": r"\(17\)(\d{6})",
#        "lot": r"\(10\)([^\(]+)",
#    }
#
#    # Extract data using regex
#    data = {}
#    for key, pattern in patterns.items():
#        match = re.search(pattern, text)
#        if match:
#            data[key] = match.group(1)
#        else:
#            data[key] = None
#
#    # Create the GS1Data model
#    return GS1Data(gtin=data["gtin"], lot=data["lot"], expiry=data["expiry"], sn=data["sn"])
#
#
# def parse_barcode(rbc: RawBarCode) -> Optional[GS1Data]:
#    # if rbc.format == zxingcpp.BarcodeFormat.UPCA:
#    #    return UPCData(text=rbc.text)
#    if rbc.format == zxingcpp.BarcodeFormat.DataMatrix:
#        return parse_gs1_data(rbc.text)
#    else:
#        # raise Exception("Unexpected Format")
#        return None


class Camera(Scanner):
    """
    A class to handle video capture from a camera.
    """

    def __init__(self, event_queue: asyncio.Queue, filename: Union[str, int] = 0) -> None:
        """
        Initialize the camera.

        :param camera_index: Index of the camera to use.
        """
        self.cap = cv2.VideoCapture(filename)
        self.lock = asyncio.Lock()
        self.event_queue = event_queue

    # async def get_frame(self) -> bytes:
    #    """
    #    Capture a frame from the camera.
    #    :return: JPEG encoded image bytes.
    #    """
    #    async with self.lock:
    #        ret, frame = self.cap.read()
    #        if not ret:
    #            return b""
    #        barcodes = zxingcpp.read_barcodes(frame)
    #        for barcode in barcodes:
    #            try:
    #                bc = parse_barcode(
    #                    RawBarCode(
    #                        text=barcode.text, format=barcode.format, content=barcode.content_type
    #                    )
    #                )
    #                if bc:
    #                    await self.event_queue.put(bc)
    #            except Exception as e:
    #                print(e)
    #        ret, jpeg = cv2.imencode(".jpg", frame)
    #        if not ret:
    #            return b""
    #        return jpeg.tobytes()

    async def release(self) -> None:
        async with self.lock:
            if self.cap.isOpened():
                self.cap.release()

    @property
    def scanner_type(self) -> ScannerType:
        return ScannerType.CAMERA

    async def shutdown(self) -> None:
        self.release

    async def scan(self) -> Optional[List[BaseBarCode]]:
        async with self.lock:
            ret, frame = self.cap.read()
            if not ret:
                return None
            barcodes = zxingcpp.read_barcodes(frame)
            bc: List[BaseBarCode] = []
            for barcode in barcodes:
                ret, jpeg = cv2.imencode(".jpg", frame)
                if not ret:
                    pass
                else:
                    bc.append(
                        BarCodeWithImage(
                            barcode_type=barcode.format, payload=barcode.text, image=jpeg.tobytes()
                        )
                    )
            if len(bc):
                return bc
            else:
                return None
