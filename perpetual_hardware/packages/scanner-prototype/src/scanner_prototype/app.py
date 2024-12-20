import asyncio
from fastapi import FastAPI, Request, Response, Depends
from fastapi.responses import StreamingResponse
from typing import AsyncGenerator
from contextlib import asynccontextmanager
import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List
from sse_starlette.sse import EventSourceResponse
import queue
from fastapi.staticfiles import StaticFiles
from starlette.responses import RedirectResponse
from perpetual_p0c.storage import (
    Cell,
    CellItem,
    CellLocationData,
    GS1Data,
    StorageModuleCellLocation,
)
from perpetual_p0c.size_database import Dimensions
from scanner_prototype.camera import Camera
from scanner_prototype.commands import BaseCommand
from scanner_prototype.deps import get_worker, get_incoming_message_queue, CommandQueue
from scanner_prototype.worker import RobotWorker

# @asynccontextmanager
# async def lifespan(app: FastAPI):
#    await load_configuration(app.state)
#    yield
#    worker.shutdown()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:
    # try:
    worker = get_worker()
    yield
    worker.shutdown()
    # except asyncio.exceptions.CancelledError as error:
    #    print(error.args)
    # finally:
    camera.release()
    #    print("Camera resource released.")


app = FastAPI(lifespan=lifespan)

event_queue: "queue.Queue[Optional[GS1Data]]" = queue.Queue()


async def gen_frames() -> AsyncGenerator[bytes, None]:
    try:
        while True:
            frame = camera.get_frame()
            if frame:
                yield (b"--frame\r\n" b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
            else:
                break
            await asyncio.sleep(0)
    except (asyncio.CancelledError, GeneratorExit):
        print("Frame generation cancelled.")
    finally:
        print("Frame generator exited.")


@app.get("/stream")
async def video_feed() -> StreamingResponse:
    return StreamingResponse(gen_frames(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/barcodes", response_class=StreamingResponse)
async def camera_events(request: Request) -> EventSourceResponse:
    async def event_generator(request: Request) -> AsyncGenerator:
        while True:
            try:
                if await request.is_disconnected():
                    break
                if not event_queue.empty():
                    event = event_queue.get()
                    if event:
                        yield {"data": event.model_dump_json()}
                    else:
                        break
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError as e:
                print(f"Disconnected from client (via refresh/close) {request.client}")
                raise e

    return EventSourceResponse(event_generator(request))


# @app.post("/api/save_dimensions")
# async def save_dimensions(dimensions: Dimensions) -> dict[str, str]:
#    print(dimensions)
#    return { "message": "great" }


@app.post("/api/store")
async def save_dimensions_and_run(
    data: GS1Data,
    dimensions: Dimensions,
    worker: RobotWorker = Depends(get_worker),
    incoming_queue: CommandQueue = Depends(get_incoming_message_queue),
) -> dict[str, str]:
    print(dimensions, data)
    worker.robot.size_db.add_item_size(data, dimensions)
    worker.robot.save_sizes()
    incoming_queue.put(BaseCommand(command="STORE_ITEM", payload=data.model_dump()))
    return {"message": "great"}


@app.get("/api/dimensions/:gs1_data")
async def get_dimensions(
    data: GS1Data, worker: RobotWorker = Depends(get_worker)
) -> Optional[Dimensions]:
    print(data)
    return worker.robot.size_db.get_item_size(data)


@app.post("/api/update_dimensions")
async def update_dimensions(
    data: GS1Data,
    dimensions: Dimensions,
    worker: RobotWorker = Depends(get_worker),
) -> dict[str, str]:
    print(data)
    worker.robot.size_db.add_item_size(data, dimensions)
    worker.robot.save_sizes()
    return {"message": "great"}


# Remove Item
@app.post("/api/clear_all")
async def remove_item(
    data: GS1Data,
    worker: RobotWorker = Depends(get_worker),
) -> dict[str, str]:
    location = worker.robot.storage.get_item_location(data)
    worker.robot.storage.clear_item(location)
    worker.robot.save_storage()
    return {"message": "great"}


# Clear All Items
@app.post("/api/remove")
async def clear_all(
    worker: RobotWorker = Depends(get_worker),
) -> dict[str, str]:
    worker.robot.storage.clear_all_items()
    worker.robot.save_storage()
    return {"message": "great"}


# Get All Items
@app.get("/api/items")
async def items(
    worker: RobotWorker = Depends(get_worker),
) -> List[CellLocationData]:
    return worker.robot.storage.get_all_cell_data()


# Retrieve Item
@app.post("/api/retrieve_items")
async def retrieve_item(
    data: GS1Data, incoming_queue: CommandQueue = Depends(get_incoming_message_queue)
) -> dict[str, str]:
    incoming_queue.put(BaseCommand(command="RETRIEVE_ITEM_GS1", payload=data.model_dump()))
    return {"message": "great"}


app.mount("/", StaticFiles(directory="./scanner-app/dist/", html=True), name="react")


@app.get("/")
async def serve_root() -> RedirectResponse:
    return RedirectResponse(url="/")


async def main() -> None:
    origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    camera = Camera(event_queue)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Server stopped by user.")
