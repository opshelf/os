from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from perpetual_os1_server.core.logging_config import configure_logging, get_logger
from perpetual_os1_server.core.robot import init_robot_worker, stop_robot_worker
from perpetual_os1_server.core.sizes import init_file_sizes, migrate_if_needed
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.core.camera import init_tool_camera_manager
from perpetual_os1_server.settings import Settings
from perpetual_os1_server.database.base import Base, engine, get_database_url
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.middleware.audit import AuditMiddleware
from perpetual_os1_server.middleware.correlation import CorrelationIdMiddleware
from perpetual_os1_server.routers import access_levels, audit, auth, camera, printer, robot, roles, sizes, storage, users
from perpetual_os1_server.settings import get_settings

configure_logging()
logger = get_logger(__name__)

sentry_dsn = os.getenv("SENTRY_DSN_API", "")
if sentry_dsn:
	from sentry_sdk.transport import HttpTransport
	from sentry_sdk.types import Event

	def before_send_with_flush(event: Event, hint: dict[str, Any]) -> Event | None:
		if "request" in event:
			request = event.get("request")
			if request is not None and isinstance(request, dict) and "headers" in request:
				headers = request.get("headers")
				if isinstance(headers, dict):
					headers.pop("Authorization", None)
					headers.pop("Cookie", None)
		return event

	sentry_sdk.init(
		dsn=sentry_dsn,
		integrations=[
			StarletteIntegration(transaction_style="endpoint"),
			FastApiIntegration(transaction_style="endpoint"),
			LoggingIntegration(event_level=None),
		],
		traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
		environment=os.getenv("DEPLOYMENT_ENVIRONMENT", "development"),
		release=os.getenv("APP_VERSION", "0.1.0"),
		before_send=before_send_with_flush,
		transport=HttpTransport,
		shutdown_timeout=2,
		debug=os.getenv("SENTRY_DEBUG", "false").lower() in ("true", "1", "yes"),
	)
	logger.info("sentry_initialized", dsn_configured=True)
else:
	logger.warning("sentry_not_configured", message="SENTRY_DSN_API not set")


def _filter_pii(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
	"""Filter sensitive information from Sentry events."""
	if "request" in event:
		if "headers" in event["request"]:
			event["request"]["headers"].pop("Authorization", None)
			event["request"]["headers"].pop("Cookie", None)
	return event


def _run_migrations() -> None:
	from alembic import command
	from alembic.config import Config

	try:
		script_location = Path(__file__).parent / "alembic"

		if script_location.exists() is False:
			logger.warning("alembic_directory_missing", script_location=str(script_location))
			return

		package_root = Path(__file__).parent.parent.parent.parent.parent
		alembic_ini_path = package_root / "alembic.ini"

		if alembic_ini_path.exists() is False:
			logger.warning("alembic_ini_missing", path=str(alembic_ini_path), action="using_fallback_config")
			alembic_cfg = Config()
			alembic_cfg.set_main_option("script_location", str(script_location))
		else:
			alembic_cfg = Config(str(alembic_ini_path))
			alembic_cfg.set_main_option("script_location", str(script_location))

		db_url = get_database_url()
		alembic_cfg.set_main_option("sqlalchemy.url", db_url)

		logger.info("running_database_migrations", script_location=str(script_location))
		command.upgrade(alembic_cfg, "head")
		logger.info("database_migrations_complete")
	except Exception as e:
		logger.error("migration_failed", error=str(e), exc_info=True)
		logger.warning("continuing_without_migrations")


def _get_tool_camera_url(settings: Settings) -> str | None:
	if settings.tool_camera.enabled is True and settings.tool_camera.rtsp_url != "":
		return settings.tool_camera.rtsp_url

	config_paths = [
		Path(__file__).parent.parent.parent / "perpetual_os1" / "src" / "perpetual_os1" / "config.json",
		Path("/app/perpetual_hardware/packages/perpetual_os1/src/perpetual_os1/config.json"),
	]

	for config_path in config_paths:
		if config_path.exists() is False:
			continue

		try:
			with open(config_path) as f:
				config = json.load(f)

			poe_camera = config.get("robot", {}).get("tool", {}).get("poe_camera", {})
			url = poe_camera.get("url") or poe_camera.get("stream_uri")
			if url is not None and url != "":
				logger.info("tool_camera_url_from_config", config_path=str(config_path), url=url)
				return str(url)
		except Exception as e:
			logger.warning("failed_to_read_config", config_path=str(config_path), error=str(e))

	return None


async def _seed_auth_if_requested() -> None:
	from sqlalchemy import select
	from perpetual_os1_server.database.models import User
	from perpetual_os1_server.database.session import AsyncSessionLocal

	if os.getenv("RUN_SEED_SCRIPT", "false").lower() not in {"1", "true", "yes"}:
		return

	script_path = Path(__file__).parent.parent.parent / "scripts" / "seed_auth.py"
	if not script_path.exists():
		logger.warning("seed_script_not_found", script_path=str(script_path))
		return

	async with AsyncSessionLocal() as db:
		result = await db.execute(select(User))
		user_count = len(result.scalars().all())

		if user_count == 0:
			logger.info("running_seed_script", script_path=str(script_path), reason="no_users_found")
			subprocess.run([sys.executable, str(script_path)], env={**os.environ, "ENV": "dev"}, check=False)
		else:
			logger.info("skipping_seed_script", user_count=user_count, reason="users_exist")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
	logger.info("application_starting", app_title=app.title)
	settings = get_settings()

	_run_migrations()

	logger.info("initializing_database_schema")
	async with engine.begin() as conn:
		await conn.run_sync(Base.metadata.create_all)

	await _seed_auth_if_requested()

	robot_started = False
	storage_override_set = False
	try:
		if settings.robot.enabled is True:
			try:
				logger.info("robot_initialization", status="starting")
				await init_robot_worker()
				robot_started = True
				logger.info("robot_worker_started", status="success")
			except Exception as e:
				logger.error(
					"robot_initialization_failed",
					error=str(e),
					error_type=type(e).__name__,
					traceback=traceback.format_exc(),
					exc_info=True
				)
				if sentry_dsn is not None:
					sentry_sdk.capture_exception(e)
		else:
			logger.info("robot_disabled", reason="settings_configuration")
	except Exception as e:
		logger.error("robot_setup_error", error=str(e), exc_info=True)
		if sentry_dsn is not None:
			sentry_sdk.capture_exception(e)

	init_file_sizes()

	tool_camera_url = _get_tool_camera_url(settings)
	if tool_camera_url is not None and tool_camera_url != "":
		try:
			await init_tool_camera_manager(tool_camera_url)
			logger.info("tool_camera_initialized", rtsp_url=tool_camera_url)
		except Exception as e:
			logger.error("tool_camera_initialization_failed", error=str(e), exc_info=True)

	async def get_storage_session() -> AsyncGenerator[StorageService, None]:
		async with AsyncSessionLocal() as session:
			await migrate_if_needed(session)
			yield StorageService(session)

	app.dependency_overrides[StorageService] = get_storage_session
	storage_override_set = True

	try:
		yield
	finally:
		logger.info("application_shutting_down")
		if robot_started is True:
			await stop_robot_worker()
			logger.info("robot_worker_stopped")
		if storage_override_set is True:
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
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(AuditMiddleware)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
	correlation_id = getattr(request.state, "correlation_id", None)

	if sentry_dsn:
		with sentry_sdk.push_scope() as scope:
			if correlation_id:
				scope.set_tag("correlation_id", correlation_id)
			scope.set_tag("path", request.url.path)
			scope.set_tag("method", request.method)
			sentry_sdk.capture_exception(exc)

	logger.error(
		"unhandled_exception",
		error=str(exc),
		error_type=type(exc).__name__,
		path=request.url.path,
		method=request.method,
		correlation_id=correlation_id,
		exc_info=True,
	)
	return JSONResponse(status_code=500, content={"detail": "Internal server error"})


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
app.include_router(printer.router, prefix="/api/print")


def start() -> None:
	import uvicorn

	log_config = uvicorn.config.LOGGING_CONFIG
	log_config["loggers"]["uvicorn.access"]["level"] = "ERROR"
	log_config["loggers"]["watchfiles"] = {"level": "ERROR"}

	uvicorn.run(
		"perpetual_os1_server.main:app",
		host="0.0.0.0",
		port=8000,
		reload=True,
		log_config=log_config,
	)
