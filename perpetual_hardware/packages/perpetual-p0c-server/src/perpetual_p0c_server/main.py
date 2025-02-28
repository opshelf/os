from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perpetual_p0c_server.core.printer.base import AbstractPrinter, ThermalPrinter
from perpetual_p0c_server.core.storage import init_file_storage
from perpetual_p0c_server.core.sizes import init_file_sizes
from perpetual_p0c_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_p0c_server.core.scanner import init_scanner_worker, stop_scanner_worker
from perpetual_p0c_server.core.workstation import init_workstation_worker, stop_workstation_worker
from perpetual_p0c_server.routers import (
    storage,
    gantry,
    sizes,
    robot,
    scanner,
    printer,
    workstation,
)
from contextlib import asynccontextmanager
from perpetual_p0c_server.core.printer import PrintWorker, MockPrinter
from typing import AsyncGenerator, Optional
from perpetual_p0c_server.settings import get_settings
import uvicorn
import logging
import sys
from perpetual_camera.worker import CameraWorker
from perpetual_camera.async_reader import AsyncCameraReader

root = logging.getLogger()
root.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root.addHandler(handler)


async def initalize_camera_worker(nats_url: str) -> Optional[CameraWorker]:
    reader = AsyncCameraReader()
    worker = CameraWorker(reader, nats_url)
    return worker



@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logging.info(f"{app.title}")
    settings = get_settings()
    logging.info(f"settings: {settings}")
    p: AbstractPrinter = MockPrinter()
    if settings.printer.enabled:
        logging.info(f"Printer Enabled Setting Up Printer")
        p = ThermalPrinter()
    print_worker = PrintWorker(
        p, settings.nats.connection_string(), "printer_queue", "printer_event_queue"
    )
    logging.info(f"Print Worker Connecting: {settings.nats.connection_string()}")
    await print_worker.connect()
    logging.info("Printer Initialized")

    logging.info(f"Camera Initializing")
    camera_worker = None
    if settings.camera.enabled:
        camera_worker = await initalize_camera_worker(settings.nats.connection_string())
        if camera_worker:
            await camera_worker.connect()
            await camera_worker.start()
            logging.info(f"Camera Initializied")
    init_file_storage()
    logging.info("Storage Initialized")
    init_file_sizes()
    logging.info("Sizes Initialized")
    await init_scanner_worker(settings.scanner.enabled)
    logging.info("Scanner Enabled")
    await init_robot_worker(settings.robot.enabled)
    logging.info("Robot Enabled")
    logging.info("Workstaton Enabled")
    await init_workstation_worker()
    yield
    await stop_robot_worker()
    await stop_scanner_worker()
    await stop_workstation_worker()
    await print_worker.close()
    if camera_worker:
        await camera_worker.stop()


app = FastAPI(lifespan=lifespan)

origins = ["http://localhost", "http://localhost:8080", "http://localhost:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(storage.router, prefix="/api/storage")
app.include_router(sizes.router, prefix="/api/sizes")
app.include_router(gantry.router, prefix="/api/gantry")
app.include_router(robot.router, prefix="/api/robot")
app.include_router(scanner.router, prefix="/api/scanner")
app.include_router(printer.router, prefix="/api/printer")
app.include_router(workstation.router, prefix="/api/workstation")


def start() -> None:
    uvicorn.run(
        "perpetual_p0c_server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=2,
    )
