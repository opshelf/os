from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional, AsyncIterator, Dict
import uvicorn
import logging
import sys
from perpetual_camera.worker import CameraWorker
from perpetual_camera.async_reader import AsyncCameraReader
from fastapi.responses import StreamingResponse
from sse_starlette import EventSourceResponse 
import asyncio
import cv2

root = logging.getLogger()
root.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root.addHandler(handler)


camera_worker: Optional[CameraWorker] = None

def get_camera_worker() -> Optional[CameraWorker]:
    global camera_worker
    return camera_worker

async def initalize_camera_worker(nats_url: str) -> Optional[CameraWorker]:
    reader = AsyncCameraReader()
    worker = CameraWorker(reader, nats_url)
    return worker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logging.info(f"{app.title}")
    logging.info(f"Camera Initializing")
    global camera_worker
    camera_worker = await initalize_camera_worker("nats://localhost:4222")
    if camera_worker:
        await camera_worker.connect()
        await camera_worker.start()
        await asyncio.sleep(1)
        camera_worker.save_baseline_image()
        logging.info(f"Camera Initializied")
    yield
    if camera_worker:
        await camera_worker.stop()





app = FastAPI(lifespan=lifespan)




app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

async def sse_task(request: Request, worker: Optional[CameraWorker]) -> AsyncIterator[Dict]:
    while True:
        print("here")
        try:
            if await request.is_disconnected():
                print("Disconnected")
                break
            if worker:
                stats = worker.stats() 
                print(stats)
                yield { "data": stats.model_dump_json() }
            await asyncio.sleep(.05)
        except asyncio.CancelledError as e:
            print(f"Disconnected from client (via refresh/close) {request.client}")
            raise e


@app.get("/stats")
async def stat_stream(request: Request, worker=Depends(get_camera_worker)) -> EventSourceResponse:
    return EventSourceResponse(sse_task(request, worker))


async def generate(request: Request, worker: Optional[CameraWorker]):
    try:
        while True:
            if worker:
                if worker.current_frame is None:
                    pass
                else:
                    ret, buffer = cv2.imencode('.jpg', worker.current_frame)
                    if not ret:
                        continue
                    frame_bytes = buffer.tobytes()
                    yield (
                        b'--frame\r\n'
                        b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
                    )

            await asyncio.sleep(.05)
    finally:
        await request.close()

@app.get("/video")
async def video(request: Request, worker=Depends(get_camera_worker)):
    return StreamingResponse(
        generate(request, worker),
        media_type="multipart/x-mixed-replace;boundary=frame",
    )

def start() -> None:
    uvicorn.run(
        "perpetual_io_detection.main:app",
        host="0.0.0.0",
        port=8001,
        reload=True,
        timeout_graceful_shutdown=2,
    )
