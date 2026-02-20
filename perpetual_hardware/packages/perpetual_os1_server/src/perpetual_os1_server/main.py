from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
import logging
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
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.core.camera import init_tool_camera_manager, init_camera_manager, init_overhead_camera_manager
from perpetual_os1_server.settings import Settings
from perpetual_os1_server.database.base import Base, engine, get_database_url
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.middleware.audit import AuditMiddleware
from perpetual_os1_server.middleware.correlation import CorrelationIdMiddleware
from perpetual_os1_server.routers import access_levels, audit, auth, calibration, camera, demo, printer, report, robot, roles, storage, system, telemetry, users
from perpetual_os1_server.settings import get_settings, get_operating_mode

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
			LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
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

	from perpetual_os1.common import CONFIG_PATH

	config_paths = [
		CONFIG_PATH,
		Path("/app/perpetual_hardware/data/config.json"),
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


def _get_overhead_camera_url(settings: Settings) -> str | None:
	if settings.overhead_camera.enabled is True and settings.overhead_camera.rtsp_url != "":
		return settings.overhead_camera.rtsp_url
	return None


def _get_io_camera_config() -> tuple[dict, dict | None] | None:
	from perpetual_os1.common import CONFIG_PATH

	if CONFIG_PATH.exists() is False:
		return None

	try:
		with open(CONFIG_PATH) as f:
			config = json.load(f)

		io_config = config.get("robot", {}).get("io", {})
		usb_camera = io_config.get("usb_camera")
		tof_camera = io_config.get("tof_camera")

		if usb_camera is not None:
			logger.info("io_camera_config_found", config_path=str(CONFIG_PATH))
			return (usb_camera, tof_camera)
	except Exception as e:
		logger.warning("failed_to_read_io_camera_config", config_path=str(CONFIG_PATH), error=str(e))

	return None


async def _ensure_developer_user() -> None:
	from sqlalchemy import select
	from argon2 import PasswordHasher
	from perpetual_os1_server.database.models import User, Role
	from perpetual_os1_server.database.session import AsyncSessionLocal

	async with AsyncSessionLocal() as db:
		result = await db.execute(
			select(User).filter(User.email == "developer@opshelf.com")
		)
		existing_user = result.scalar_one_or_none()

		if existing_user is not None:
			logger.info("developer_user_exists", email="developer@opshelf.com")
			return

		role_result = await db.execute(select(Role).filter(Role.name == "admin"))
		admin_role = role_result.scalar_one_or_none()

		if admin_role is None:
			logger.warning("admin_role_not_found", action="skipping_developer_user_creation")
			return

		ph = PasswordHasher()
		pin_hash = ph.hash("1111")

		developer_user = User(
			email="developer@opshelf.com",
			first_name="Developer",
			last_name="System",
			pin_hash=pin_hash,
			role_id=admin_role.id,
			is_active=True,
			is_system=True,
		)

		db.add(developer_user)
		await db.commit()
		logger.info("developer_user_created", email="developer@opshelf.com")


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


async def _seed_demo_if_needed() -> None:
	from perpetual_os1.common import OperatingMode
	from sqlalchemy import select, func
	from perpetual_os1_server.database.models import Item

	mode = get_operating_mode()
	if mode != OperatingMode.demo:
		return

	async with AsyncSessionLocal() as db:
		result = await db.execute(select(func.count()).select_from(Item))
		count = result.scalar_one()

		if count > 0:
			logger.info("demo_seed_skipped", reason="items_exist", count=count)
			return

	script_path = Path(__file__).parent.parent.parent / "scripts" / "demo_seed.py"
	if script_path.exists() is False:
		logger.warning("demo_seed_script_not_found", script_path=str(script_path))
		return

	logger.info("running_demo_seed", script_path=str(script_path))
	result = subprocess.run(
		[sys.executable, str(script_path)],
		env={**os.environ},
		check=False,
		capture_output=True,
		text=True,
	)

	if result.returncode == 0:
		logger.info("demo_seed_complete", stdout=result.stdout.strip())
	else:
		logger.error("demo_seed_failed", returncode=result.returncode, stderr=result.stderr.strip())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
	logger.info("application_starting", app_title=app.title)
	settings = get_settings()

	_run_migrations()

	logger.info("initializing_database_schema")
	async with engine.begin() as conn:
		await conn.run_sync(Base.metadata.create_all)

	await _seed_auth_if_requested()
	await _ensure_developer_user()
	await _seed_demo_if_needed()

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

	tool_camera_url = _get_tool_camera_url(settings)
	if tool_camera_url is not None and tool_camera_url != "":
		try:
			await init_tool_camera_manager(tool_camera_url)
			logger.info("tool_camera_initialized", rtsp_url=tool_camera_url)
		except Exception as e:
			logger.error("tool_camera_initialization_failed", error=str(e), exc_info=True)

	overhead_camera_url = _get_overhead_camera_url(settings)
	if overhead_camera_url is not None and overhead_camera_url != "":
		try:
			await init_overhead_camera_manager(overhead_camera_url)
			logger.info("overhead_camera_initialized", rtsp_url=overhead_camera_url)
		except Exception as e:
			logger.error("overhead_camera_initialization_failed", error=str(e), exc_info=True)

	if settings.camera.enabled is True:
		io_camera_config = _get_io_camera_config()
		if io_camera_config is not None:
			try:
				from hardware.iocam import AFSCam
				from perpetual_os1.common import UsbCamera, TofCamera

				usb_config, tof_config = io_camera_config
				usb_params = UsbCamera(**usb_config)
				tof_params = TofCamera(**tof_config) if tof_config is not None and settings.camera.depth_enabled is True else None

				io_camera = AFSCam(usb_params, tof_params)
				await io_camera.start(enable_tof=settings.camera.depth_enabled)
				await init_camera_manager(io_camera, depth_enabled=settings.camera.depth_enabled)
				logger.info("io_camera_initialized", depth_enabled=settings.camera.depth_enabled)
			except Exception as e:
				logger.error("io_camera_initialization_failed", error=str(e), exc_info=True)
		else:
			logger.warning("io_camera_config_not_found")

	async def get_storage_session() -> AsyncGenerator[StorageService, None]:
		async with AsyncSessionLocal() as session:
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
	detail: dict[str, str] = {"message": "Internal server error", "code": "internal_error"}
	if correlation_id:
		detail["correlation_id"] = correlation_id
		detail["reason"] = f"correlation_id={correlation_id}"
	return JSONResponse(status_code=500, content={"detail": detail})


@app.get("/api/health")
async def health() -> dict[str, str]:
	return {"status": "ok", "mode": get_operating_mode().value}


@app.get("/api/system/info")
async def system_info() -> dict[str, str | None]:
	import fcntl
	import socket
	import struct

	lan_ip: str | None = None
	try:
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		lan_ip = socket.inet_ntoa(
			fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", b"wlan0"))[20:24]
		)
		s.close()
	except OSError:
		pass

	return {"lan_ip": lan_ip}


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(access_levels.router)
app.include_router(audit.router)
app.include_router(camera.router, prefix="/api/camera")
app.include_router(robot.router, prefix="/api/robot")
app.include_router(storage.router, prefix="/api/storage")
app.include_router(printer.router, prefix="/api/print")
app.include_router(calibration.router)
app.include_router(demo.router)
app.include_router(system.router, prefix="/api/system")
app.include_router(report.router, prefix="/api")
app.include_router(telemetry.router, prefix="/api")


def start() -> None:
	import uvicorn

	log_config = uvicorn.config.LOGGING_CONFIG
	log_config["loggers"]["uvicorn.access"]["level"] = "ERROR"
	log_config["loggers"]["watchfiles"] = {"level": "ERROR"}

	import os

	reload_enabled = os.environ.get("UVICORN_RELOAD", "false").lower() == "true"

	uvicorn.run(
		"perpetual_os1_server.main:app",
		host="0.0.0.0",
		port=8000,
		reload=reload_enabled,
		reload_excludes=["*.json", "*.db", "*.db-shm", "*.db-wal"] if reload_enabled else None,
		log_config=log_config,
	)
