from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perpetual_os1_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_os1_server.routers import (
    gantry,
    robot,
)
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from perpetual_p0c_server.settings import get_settings
import uvicorn
import logging
import sys

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
    await init_robot_worker()
    logging.info("Robot Enabled")
    logging.info("Workstaton Enabled")
    yield
    await stop_robot_worker()


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


def start() -> None:
    uvicorn.run(
        "perpetual_os1_server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        timeout_graceful_shutdown=2,
    )
