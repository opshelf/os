from __future__ import annotations

import asyncio
import os
import signal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from openshelf_os.common import OperatingMode
from openshelf_os_server.core.logging_config import get_logger
from openshelf_os_server.settings import get_operating_mode, get_root_config, MODE_FILE, save_root_config
from openshelf_os_server.routers.auth_deps import get_current_user, require_permissions

logger = get_logger(__name__)

router = APIRouter(tags=["system"])


class ModeResponse(BaseModel):
	mode: str


class ModeUpdateRequest(BaseModel):
	mode: str


class ModeUpdateResponse(BaseModel):
	mode: str
	message: str


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


@router.put(
	"/mode",
	response_model=ModeUpdateResponse,
	dependencies=[Depends(require_permissions("system:admin"))],
)
async def set_mode(request: ModeUpdateRequest) -> ModeUpdateResponse:
	raw = request.mode.strip().lower()

	valid_modes = {m.value for m in OperatingMode}
	if raw not in valid_modes:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Invalid mode: {raw!r}. Must be one of: {', '.join(sorted(valid_modes))}",
		)

	current = get_operating_mode()
	if raw == current.value:
		raise HTTPException(
			status_code=status.HTTP_409_CONFLICT,
			detail=f"Already in {raw} mode",
		)

	MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
	MODE_FILE.write_text(raw, encoding="utf-8")

	logger.info("operating_mode_changed", previous=current.value, new=raw)
	_schedule_api_restart()

	return ModeUpdateResponse(
		mode=raw,
		message="Mode set. Restarting...",
	)


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
