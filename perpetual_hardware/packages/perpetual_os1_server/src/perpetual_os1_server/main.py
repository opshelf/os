from __future__ import annotations

import logging
import os
import subprocess
import sys
import traceback
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
from perpetual_os1_server.routers import access_levels, audit, auth, camera, robot, roles, sizes, storage, users
from perpetual_os1_server.settings import get_settings

root = logging.getLogger()
root.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
handler.setFormatter(formatter)
root.addHandler(handler)

logging.getLogger("aiosqlite").setLevel(logging.WARNING)


def _run_migrations() -> None:
	from alembic import command
	from alembic.config import Config
	from perpetual_os1_server.database.base import get_database_url

	try:
		script_location = Path(__file__).parent / "alembic"

		if script_location.exists() == False:
			logging.warning("Alembic directory not found at %s", script_location)
			return

		package_root = Path(__file__).parent.parent.parent.parent.parent
		alembic_ini_path = package_root / "alembic.ini"

		if alembic_ini_path.exists() == False:
			logging.warning("alembic.ini not found at %s, using fallback", alembic_ini_path)
			alembic_cfg = Config()
			alembic_cfg.set_main_option("script_location", str(script_location))
		else:
			alembic_cfg = Config(str(alembic_ini_path))
			alembic_cfg.set_main_option("script_location", str(script_location))

		db_url = get_database_url()
		alembic_cfg.set_main_option("sqlalchemy.url", db_url)

		logging.info("Running database migrations from %s", script_location)
		command.upgrade(alembic_cfg, "head")
		logging.info("Database migrations complete")
	except Exception as e:
		logging.error("Migration failed: %s", str(e), exc_info=True)
		logging.warning("Continuing without migrations")


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

    _run_migrations()

    logging.info("Initializing database schema")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _seed_auth_if_requested()

    robot_started = False
    storage_override_set = False
    try:
        if settings.robot.enabled:
            try:
                logging.info("Robot is enabled, attempting initialization")
                await init_robot_worker()
                robot_started = True
                logging.info("Robot worker started successfully")
            except Exception as e:
                logging.error("Robot initialization failed: %s", str(e))
                logging.error("Exception type: %s", type(e).__name__)
                logging.error("Full traceback: %s", traceback.format_exc())
        else:
            logging.info("Robot is disabled in settings")
    except Exception as e:
        logging.error("Unexpected error during robot setup: %s", str(e))

        init_file_sizes()

        async def get_storage_session() -> AsyncGenerator[StorageService, None]:
            async with AsyncSessionLocal() as session:
                await migrate_if_needed(session)
                yield StorageService(session)

        app.dependency_overrides[StorageService] = get_storage_session
        storage_override_set = True

    try:
        yield
    finally:
        logging.info("Shutting down")
        if robot_started:
            await stop_robot_worker()
            logging.info("Robot worker stopped")
        if storage_override_set:
            app.dependency_overrides.pop(StorageService, None)


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
app.include_router(access_levels.router)
app.include_router(audit.router)
app.include_router(camera.router, prefix="/api/camera")
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
