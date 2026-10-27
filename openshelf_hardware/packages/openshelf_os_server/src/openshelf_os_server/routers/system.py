from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import asyncio
import os
import signal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

import sentry_sdk
from openshelf_os.operating_mode import is_sentry_reporting_enabled, set_sentry_reporting_enabled, should_enable_sentry

from openshelf_os_server.core.logging_config import get_logger
from openshelf_os_server.settings import get_operating_mode, get_root_config, save_root_config
from openshelf_os_server.routers.auth_deps import get_current_user, require_permissions

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


class ModeResponse(BaseModel):
	mode: str


class HomeOnInitResponse(BaseModel):
	home_on_init: bool


class HomeOnInitUpdateRequest(BaseModel):
	home_on_init: bool


class SentryReportingResponse(BaseModel):
	enabled: bool
	default: bool
	initialized: bool


class SentryReportingUpdateRequest(BaseModel):
	enabled: bool


class RestartResponse(BaseModel):
	message: str


def _restart_process_id() -> int:
	if os.path.exists("/.dockerenv") is True:
		return 1
	return os.getpid()


def _schedule_api_restart() -> None:
	asyncio.get_running_loop().call_later(1.0, os.kill, _restart_process_id(), signal.SIGTERM)

@router.get(
    "/mode",
    response_model=ModeResponse,
    dependencies=[Depends(get_current_user)],
    summary="Read the operating mode",
    responses=response_docs(errors=(401,)),
)
async def get_mode() -> ModeResponse:
	"""Read the operating mode.

	Returns the current production, development, demo, or mock mode. Mode is selected at
	process startup; the API has no mode-changing endpoint. Use the repository startup
	tooling to change it.
	"""
	mode = get_operating_mode()
	return ModeResponse(mode=mode.value)


@router.get(
    "/home-on-init",
    response_model=HomeOnInitResponse,
    dependencies=[Depends(require_permissions("robot:control"))],
    summary="Read startup homing policy",
    responses=response_docs(errors=(401, 403)),
)
async def get_home_on_init() -> HomeOnInitResponse:
	"""Read startup homing policy.

	Requires `robot:control`. Returns whether initialization is configured to home the
	robot; does not move hardware.
	"""
	config = get_root_config()
	return HomeOnInitResponse(home_on_init=config.robot_app.home_on_init)


@router.put(
    "/home-on-init",
    response_model=HomeOnInitResponse,
    dependencies=[Depends(require_permissions("robot:control"))],
    summary="Set startup homing policy",
    responses=response_docs(errors=(401, 403)),
)
async def set_home_on_init(request: HomeOnInitUpdateRequest) -> HomeOnInitResponse:
	"""Set startup homing policy.

	Requires `robot:control`. Persists whether robot initialization should home hardware.
	Does not home the robot immediately.
	"""
	config = get_root_config()
	config.robot_app.home_on_init = request.home_on_init
	save_root_config(config)
	logger.info("home_on_init_changed", home_on_init=request.home_on_init)
	return HomeOnInitResponse(home_on_init=config.robot_app.home_on_init)


def _sentry_reporting_response() -> SentryReportingResponse:
	return SentryReportingResponse(
		enabled=is_sentry_reporting_enabled(),
		default=should_enable_sentry(),
		initialized=sentry_sdk.is_initialized(),
	)


@router.get(
    "/sentry-reporting",
    response_model=SentryReportingResponse,
    dependencies=[Depends(require_permissions("robot:control"))],
    summary="Read Sentry error reporting state",
    responses=response_docs(errors=(401, 403)),
)
async def get_sentry_reporting() -> SentryReportingResponse:
	"""Read Sentry error reporting state.

	Requires `robot:control`. `enabled` is the live state, `default` is what it resets to
	on API restart (on in production and demo, off otherwise), and `initialized` is false
	when no Sentry DSN is configured for this process, in which case nothing is sent.
	"""
	return _sentry_reporting_response()


@router.put(
    "/sentry-reporting",
    response_model=SentryReportingResponse,
    dependencies=[Depends(require_permissions("robot:control"))],
    summary="Enable or disable Sentry error reporting",
    responses=response_docs(errors=(401, 403)),
)
async def set_sentry_reporting(request: SentryReportingUpdateRequest) -> SentryReportingResponse:
	"""Enable or disable Sentry error reporting.

	Requires `robot:control`. Gates every Sentry event from the API and robot worker,
	including hardware crash video recordings. Not persisted: an API restart restores the
	operating mode default.
	"""
	set_sentry_reporting_enabled(request.enabled)
	logger.info("sentry_reporting_changed", enabled=request.enabled)
	return _sentry_reporting_response()


@router.post(
    "/restart",
    response_model=RestartResponse,
    dependencies=[Depends(require_permissions("robot:control"))],
    summary="Request an API restart",
    responses=response_docs(errors=(401, 403)),
)
async def restart_api() -> RestartResponse:
	"""Request an API restart.

	Schedules a process exit so the container supervisor can restart the service. The
	response is sent before the restart; active connections can then close. Does not change
	the operating mode. Reconnect and check health before resuming work.
	"""
	logger.info("api_restart_requested")
	_schedule_api_restart()
	return RestartResponse(message="Restarting API container...")
