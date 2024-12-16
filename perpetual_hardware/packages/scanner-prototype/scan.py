import asyncio
from fastapi import FastAPI, Response, Request
from fastapi.responses import StreamingResponse
import cv2
import threading
from typing import AsyncGenerator
from contextlib import asynccontextmanager
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, Union
import zxingcpp
import cv2
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel
import queue

class GS1Data(BaseModel):
    gtin: str
    expiry: str
    lot: str
    sn: str

class UPCData(BaseModel):
    gtin: str


BarCode = Union[GS1Data, UPCData]


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        yield
    except asyncio.exceptions.CancelledError as error:
        print(error.args)
    finally:
        camera.release()
        print("Camera resource released.")

app = FastAPI(lifespan=lifespan)

event_queue = queue.Queue()


class Camera:
    """
    A class to handle video capture from a camera.
    """

    def __init__(self, event_queue: queue.Queue, url: Optional[Union[str, int]] = 0) -> None:
        """
        Initialize the camera.

        :param camera_index: Index of the camera to use.
        """
        self.cap = cv2.VideoCapture(url)
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
                return b''

            barcodes = zxingcpp.read_barcodes(frame)
            self.event_queue.put(GS1Data(
                gtin="",
                expiry="",
                lot="",
                sn=""
            ))
            for barcode in barcodes:
                print(
                    "Found barcode:"
                    f'\n Text:    "{barcode.text}"'
                    f"\n Format:   {barcode.format}"
                    f"\n Content:  {barcode.content_type}"
                    f"\n Position: {barcode.position}"
                )
            ret, jpeg = cv2.imencode('.jpg', frame)

            if not ret:
                return b''

            return jpeg.tobytes()

    def release(self) -> None:
        with self.lock:
            if self.cap.isOpened():
                self.cap.release()


async def gen_frames() -> AsyncGenerator[bytes, None]:
    try:
        while True:
            frame = camera.get_frame()
            if frame:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            else:
                break
            await asyncio.sleep(0)
    except (asyncio.CancelledError, GeneratorExit):
        print("Frame generation cancelled.")
    finally:
        print("Frame generator exited.")


@app.get("/stream")
async def video_feed() -> StreamingResponse:
    return StreamingResponse(
        gen_frames(),
        media_type='multipart/x-mixed-replace; boundary=frame'
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
    #return StreamingResponse(event_generator(), media_type="text/event-stream")
    return EventSourceResponse(event_generator(request))

async def main():
    origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    config = uvicorn.Config(app, host='0.0.0.0', port=8000,
        reload=True,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == '__main__':
    camera = Camera(event_queue)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server stopped by user.")