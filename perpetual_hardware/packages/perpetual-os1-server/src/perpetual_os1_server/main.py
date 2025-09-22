from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from perpetual_os1_server.core.automatic import AutomaticStorage
from perpetual_os1_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_os1_server.core.scale import init_scale_worker, stop_scale_worker
from perpetual_os1_server.core.scanner import init_scanner_worker, stop_scanner_worker
from perpetual_os1_server.routers import gantry, robot, scale, scanner, printer, sizes, storage
from perpetual_os1_server.routers import auth, users, roles, audit
from perpetual_os1_server.core.sizes import init_file_sizes, migrate_if_needed
from perpetual_os1_server.database.base import engine, Base
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.middleware.audit import AuditMiddleware
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from perpetual_os1_server.settings import get_settings
from sqlalchemy.ext.asyncio import AsyncSession
from perpetual_os1_server.core.storage import StorageService
import uvicorn
import logging
import sys
import asyncio
import os

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
    
    logging.info("Initializing database...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    if os.getenv("RUN_SEED_SCRIPT") == "true":
        logging.info("Running seed script...")
        # Run seed script as subprocess
        import subprocess
        import sys
        from pathlib import Path
        script_path = Path(__file__).parent.parent.parent / "scripts" / "seed_auth.py"
        if script_path.exists():
            subprocess.run([sys.executable, str(script_path)], env={**os.environ, "ENV": "dev"})
    
    if settings.robot.enabled:
        await init_robot_worker()
        logging.info("Robot Enabled")
    
    if settings.scale.enabled:
        await init_scale_worker()
        logging.info("Scale Enabled")
    
    if settings.scanner.enabled:
        await init_scanner_worker()
        logging.info("Scanner Enabled")

    init_file_sizes()

    # Initialize storage service dependency
    async def get_storage_session() -> AsyncGenerator[StorageService, None]:
        async with AsyncSessionLocal() as session:
            await migrate_if_needed(session)
            storage_service = StorageService(session)
            yield storage_service

    app.dependency_overrides[StorageService] = get_storage_session
    
    # AutomaticStorage requires NATS which may not be available in dev
    auto_storage = None
    try:
        auto_storage = AutomaticStorage()
        await auto_storage.connect()
        asyncio.create_task(auto_storage.run())
    except Exception as e:
        logging.warning(f"AutomaticStorage disabled: {e}")
    yield
    logging.info("Shutting Down")
    if settings.robot.enabled:
        await stop_robot_worker()
        logging.info("Robot Shutdown")
    
    if settings.scale.enabled:
        await stop_scale_worker()
        logging.info("Scale Shutdown")
    
    if settings.scanner.enabled:
        await stop_scanner_worker()
        logging.info("Scanner Shutdown")
    if auto_storage is not None:
        await auto_storage.close()


app = FastAPI(
    lifespan=lifespan,
    title="Perpetual OS1 Server API",
    description="API for controlling Perpetual OS1 robotic automation system including gantry, storage, scanner, and scale operations",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
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
async def health():
	return {"status": "ok"}

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(gantry.router, prefix="/api")
app.include_router(robot.router, prefix="/api/robot")
app.include_router(scale.router, prefix="/api/scale")
app.include_router(scanner.router, prefix="/api/scanner")
app.include_router(printer.router, prefix="/api/print")
app.include_router(sizes.router, prefix="/api/sizes")
app.include_router(storage.router, prefix="/api/storage")


def start() -> None:
    uvicorn.run(
        "perpetual_os1_server.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        #timeout_graceful_shutdown=2,
    )
