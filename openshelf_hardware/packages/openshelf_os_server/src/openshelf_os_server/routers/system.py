from __future__ import annotations

import asyncio
import os
import signal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

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


class RestartResponse(BaseModel):
	message: str


def _restart_process_id() -> int:
	if os.path.exists("/.dockerenv") is True:
		return 1
	return os.getpid()


def _schedule_api_restart() -> None:
	asyncio.get_running_loop().call_later(1.0, os.kill, _restart_process_id(), signal.SIGTERM)

@router.get("/mode", response_model=ModeResponse, dependencies=[Depends(get_current_user)])
async def get_mode() -> ModeResponse:
	mode = get_operating_mode()
	return ModeResponse(mode=mode.value)


@router.get(
	"/home-on-init",
	response_model=HomeOnInitResponse,
	dependencies=[Depends(require_permissions("robot:control"))],
)
async def get_home_on_init() -> HomeOnInitResponse:
	config = get_root_config()
	return HomeOnInitResponse(home_on_init=config.robot_app.home_on_init)


@router.put(
	"/home-on-init",
	response_model=HomeOnInitResponse,
	dependencies=[Depends(require_permissions("robot:control"))],
)
async def set_home_on_init(request: HomeOnInitUpdateRequest) -> HomeOnInitResponse:
	config = get_root_config()
	config.robot_app.home_on_init = request.home_on_init
	save_root_config(config)
	logger.info("home_on_init_changed", home_on_init=request.home_on_init)
	return HomeOnInitResponse(home_on_init=config.robot_app.home_on_init)


@router.post(
	"/restart",
	response_model=RestartResponse,
	dependencies=[Depends(get_current_user)],
)
async def restart_api() -> RestartResponse:
	logger.info("api_restart_requested")
	_schedule_api_restart()
	return RestartResponse(message="Restarting API container...")
