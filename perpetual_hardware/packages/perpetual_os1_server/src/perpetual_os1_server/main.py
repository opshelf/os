from __future__ import annotations

import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from perpetual_os1_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_os1_server.core.sizes import init_file_sizes, migrate_if_needed
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.database.base import Base, engine
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.middleware.audit import AuditMiddleware
from perpetual_os1_server.routers import audit, auth, robot, roles, sizes, storage, users
from perpetual_os1_server.settings import get_settings

root = logging.getLogger()
root.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root.addHandler(handler)

logging.getLogger("aiosqlite").setLevel(logging.WARNING)


async def _seed_auth_if_requested() -> None:
    from sqlalchemy import select
    from perpetual_os1_server.database.models import User
    from perpetual_os1_server.database.session import AsyncSessionLocal

    if os.getenv("RUN_SEED_SCRIPT", "false").lower() not in {"1", "true", "yes"}:
        return

    script_path = Path(__file__).parent.parent.parent / "scripts" / "seed_auth.py"
    if not script_path.exists():
        logging.warning("Seed script expected at %s but not found", script_path)
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        user_count = len(result.scalars().all())

        if user_count == 0:
            logging.info("No users found, running seed script %s", script_path)
            subprocess.run([sys.executable, str(script_path)], env={**os.environ, "ENV": "dev"}, check=False)
        else:
            logging.info("Found %d existing users, skipping seed script", user_count)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logging.info("Starting %s", app.title)
    settings = get_settings()

    logging.info("Initializing database schema")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _seed_auth_if_requested()

    robot_started = False
    try:
        if settings.robot.enabled:
            try:
                await init_robot_worker()
                robot_started = True
                logging.info("Robot worker started")
            except Exception as e:
                logging.error("Robot initialization failed: %s", str(e))

        init_file_sizes()

        async def get_storage_session() -> AsyncGenerator[StorageService, None]:
            async with AsyncSessionLocal() as session:
                await migrate_if_needed(session)
                yield StorageService(session)

        app.dependency_overrides[StorageService] = get_storage_session
        yield
    finally:
        logging.info("Shutting down")
        if robot_started:
            await stop_robot_worker()
            logging.info("Robot worker stopped")


app = FastAPI(
    lifespan=lifespan,
    title="Perpetual OS1 Server API",
    description="API for controlling Perpetual OS1 robotic automation system",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditMiddleware)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(robot.router, prefix="/api/robot")
app.include_router(sizes.router, prefix="/api/sizes")
app.include_router(storage.router, prefix="/api/storage")


def start() -> None:
    import uvicorn

    uvicorn.run(
        "perpetual_os1_server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
