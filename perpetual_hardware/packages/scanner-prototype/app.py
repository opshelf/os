"""
import base64
import time
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
import numpy as np
import cv2
import json
import time
from typing import Optional
import threading
import zxingcpp
from dataclasses import dataclass
import cv2
import threading
import asyncio
import uvicorn
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
import queue


class GS1Data(BaseModel):
    gtin: str
    expiry: str
    lot: str
    sn: str


class CameraDriver(threading.Thread):
    def __init__(self, event_queue: queue.Queue):
        print("Camera Initialized")
        super().__init__()
        self.daemon = True
        self._event_queue = event_queue
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
                    self._event_queue.put(GS1Data(gtin="", expiry="", lot="", sn=""))
                    for barcode in barcodes:
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
        print("Shut Down")
        self._stop_event.set()
        self._cap.release()


event_queue = queue.Queue()

# Initialize and start the camera at startup
camera = CameraDriver(event_queue)


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting")
    camera.start()
    yield
    event_queue.put(None)  # Notify the generator to stop
    camera.stop()
    print("Ending")


app = FastAPI(lifespan=lifespan)

origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/barcodes", response_class=StreamingResponse)
async def camera_events(request: Request):
    print("Barcode Streams")

    async def event_generator(request: Request):
        while True:
            try:
                if await request.is_disconnected():
                    break
                event = event_queue.get()
                if not event_queue.empty():
                    yield {"data": event.model_dump_json()}
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError as e:
                print(f"Disconnected from client (via refresh/close) {request.client}")
                raise e

    # return StreamingResponse(event_generator(), media_type="text/event-stream")
    return EventSourceResponse(event_generator(request))


@app.get("/stream", response_class=StreamingResponse)
async def stream_sse():
    async def event_generator():
        while True:
            # Grab the latest frame from the camera
            frame = camera.frame
            if frame is not None:
                # Encode the frame to PNG and then base64
                (_, buffer) = cv2.imencode(".jpg", frame)
                buffer_frame = buffer.tobytes()
                yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + buffer_frame + b"\r\n")
            time.sleep(0.1)

    return StreamingResponse(
        event_generator(), media_type="multipart/x-mixed-replace;boundary=frame"
    )


def generate(request: Request, camera: CameraDriver):
    while camera.is_alive():
        if camera.frame is not None:
            (_, buffer) = cv2.imencode(".jpg", camera.frame)
            buffer_frame = buffer.tobytes()
            yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + buffer_frame + b"\r\n")
        time.sleep(0.1)
    print("Camera Not Alive")


def start() -> None:
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=2,
    )
"""
