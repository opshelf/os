from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perpetual_os1_server.core.automatic import AutomaticStorage
from perpetual_os1_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_os1_server.core.scale import init_scale_worker, stop_scale_worker
from perpetual_os1_server.core.scanner import init_scanner_worker, stop_scanner_worker
from perpetual_os1_server.routers import gantry, robot, scale, scanner, printer, sizes
from perpetual_os1_server.core.sizes import init_file_sizes
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from perpetual_p0c_server.settings import get_settings
import uvicorn
import logging
import sys
import asyncio

root = logging.getLogger()
root.setLevel(logging.INFO)

handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root.addHandler(handler)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logging.info(f"{app.title}")
    settings = get_settings()
    logging.info(f"settings: {settings}")
    # await init_robot_worker()
    logging.info("Robot Enabled")
    # await init_scale_worker()
    logging.info("Scale Enabled")
    # await init_scanner_worker()
    logging.info("Scanner Enabled")
    init_file_sizes()
    auto_storage = AutomaticStorage()
    # await auto_storage.connect()
    asyncio.create_task(auto_storage.run())
    yield
    logging.info("Shutting Down")
    # await stop_robot_worker()
    logging.info("Robot Shutdown")
    # await stop_scale_worker()
    logging.info("Scale Shutdown")
    # await stop_scanner_worker()
    logging.info("Scanner Shutdown")
    # await auto_storage.close()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(gantry.router, prefix="/api")
app.include_router(robot.router, prefix="/api/robot")
app.include_router(scale.router, prefix="/api/scale")
app.include_router(scanner.router, prefix="/api/scanner")
app.include_router(printer.router, prefix="/api/print")
app.include_router(sizes.router, prefix="/api/sizes")


def start() -> None:
    uvicorn.run(
        "perpetual_os1_server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=2,
    )
