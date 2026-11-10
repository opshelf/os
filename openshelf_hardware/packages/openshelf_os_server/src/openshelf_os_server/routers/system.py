from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import asyncio
import functools
import os
import signal
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

import sentry_sdk
from openshelf_os.operating_mode import is_sentry_reporting_enabled, set_sentry_reporting_enabled, should_enable_sentry

from openshelf_os_server.core.logging_config import get_logger
from openshelf_os.config.exceptions import ConfigRequirementError, ConfigValidationError
from openshelf_os.config.root import RootConfig
from openshelf_os.config.store import ConfigConflictError

from openshelf_os_server.core.init_progress import OVERALL_INITIALIZING, get_init_tracker
from openshelf_os_server.core.laser_auto_calibration import is_laser_auto_calibration_running
from openshelf_os_server.core.manual_motion import ensure_robot_available_for_manual_motion, get_manual_motion_operation
from openshelf_os_server.core.robot import get_robot_worker
from openshelf_os_server.errors import CODE_MESSAGES, api_error
from openshelf_os_server.settings import (
	get_config_store,
	get_operating_mode,
	get_root_config,
	replace_root_config,
	update_root_config,
)
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
    "/error-messages",
    response_model=dict[str, str],
    summary="List the operator sentence for each error code",
)
async def get_error_messages() -> dict[str, str]:
    """List the operator sentence for each error code.

    The machine owns this copy: a client that shows its own sentence for a code (instead
    of `detail.message`) reads it here, so the kiosk and this API cannot drift apart.
    Public, because sign-in failures need it before there is a session.
    """
    return CODE_MESSAGES


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
	config = update_root_config(lambda draft: setattr(draft.robot_app, "home_on_init", request.home_on_init))
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
    responses=response_docs(errors=(401, 403, 409)),
)
async def restart_api() -> RestartResponse:
	"""Request an API restart.

	Refuses with `409` while the robot moves, has queued work, runs the demo cycle or
	calibrates. Schedules a process exit so the container supervisor can restart the service. The
	response is sent before the restart; active connections can then close. Does not change
	the operating mode. Reconnect and check health before resuming work.
	"""
	await _ensure_safe_to_restart()
	logger.info("api_restart_requested")
	_schedule_api_restart()
	return RestartResponse(message="Restarting API container...")


# `auth` carries the JWT secret and is driven by the environment; the schema version belongs to
# migrations. Neither is editable. `storage` changes must go through the storage routes, which refuse
# a relayout of an occupied tray; a saved layout that conflicts with stored bins crash-loops startup.
_UNEDITABLE_SECTIONS = frozenset({"auth", "schema_version", "storage"})


class ConfigSectionResponse(BaseModel):
	section: str
	file: str
	revision: str
	data: Any


class ConfigIssue(BaseModel):
	path: str
	message: str


class ConfigValidateRequest(BaseModel):
	section: str = "robot"
	data: Any


class ConfigValidateResponse(BaseModel):
	valid: bool
	issues: list[ConfigIssue]


class ConfigSaveRequest(BaseModel):
	section: str = "robot"
	data: Any
	revision: str


class ConfigSaveResponse(BaseModel):
	revision: str
	restarting: bool


def _editable_section(section: str) -> str:
	if section in _UNEDITABLE_SECTIONS or section not in RootConfig.model_fields:
		raise api_error(status.HTTP_404_NOT_FOUND, "config_section_unknown", "")
	return section


@functools.cache
def _root_schema() -> dict[str, Any]:
	return RootConfig.model_json_schema()


# Developer tool: rewrites any editable section (printer, storage limits), so it needs an admin
# permission on top of robot:control. Holding roles:write already allows self-granting anything.
CONFIG_EDITOR_PERMISSIONS = ["robot:control", "roles:write"]


def _config_issues(document: dict[str, Any]) -> list[ConfigIssue]:
	try:
		get_config_store().validate(document)
	except ConfigValidationError as exc:
		return [
			ConfigIssue(path=".".join(str(part) for part in error.get("loc", ())), message=str(error.get("msg", "")))
			for error in exc.errors
		]
	except ConfigRequirementError as exc:
		return [ConfigIssue(path=path, message=message) for path, message in exc.issues]
	return []


async def _ensure_safe_to_restart() -> None:
	if await is_laser_auto_calibration_running() is True:
		raise api_error(status.HTTP_409_CONFLICT, "calibration_running", "")
	if get_manual_motion_operation() is not None:
		raise api_error(status.HTTP_409_CONFLICT, "movement_in_progress", "")
	# The startup home runs before the worker is published and holds neither lock above.
	if get_init_tracker().snapshot()["phase"] == OVERALL_INITIALIZING:
		raise api_error(status.HTTP_409_CONFLICT, "robot_not_initialized", "")
	try:
		worker = await get_robot_worker()
	except ValueError:
		return
	# A safety interruption holds the robot still; restarting is safe, and the fix for a wrong
	# e-stop or GPIO setting may be exactly this save. Parked or queued work still blocks.
	ensure_robot_available_for_manual_motion(worker, allow_safety_interruption=True)


@router.get(
    "/config",
    response_model=ConfigSectionResponse,
    dependencies=[Depends(require_permissions(CONFIG_EDITOR_PERMISSIONS))],
    summary="Read one section of the config file",
    responses=response_docs(errors=(401, 403, 404)),
)
async def get_config_section(section: str = Query("robot")) -> ConfigSectionResponse:
	"""Read one section of the config file.

	Requires `robot:control` and `roles:write`. Returns the section as stored on disk (no environment overrides,
	no defaults filled in) and the file revision a later save must quote. `auth` and
	`schema_version` are not readable here.
	"""
	_editable_section(section)
	store = get_config_store()
	document = store.document()
	return ConfigSectionResponse(
		section=section,
		file=store.path.name,
		revision=document.revision,
		data=document.data.get(section),
	)


@router.get(
    "/config/schema",
    response_model=dict[str, Any],
    dependencies=[Depends(require_permissions(CONFIG_EDITOR_PERMISSIONS))],
    summary="Read the JSON schema of one config section",
    responses=response_docs(errors=(401, 403, 404)),
)
async def get_config_schema(section: str = Query("robot")) -> dict[str, Any]:
	"""Read the JSON schema of one config section.

	Requires `robot:control` and `roles:write`. Returns the section's schema with the shared `$defs`. It covers
	types, bounds and choices; rules across fields are checked by `POST /config/validate`.
	"""
	_editable_section(section)
	schema = _root_schema()
	return {**schema["properties"][section], "$defs": schema.get("$defs", {})}


@router.post(
    "/config/validate",
    response_model=ConfigValidateResponse,
    dependencies=[Depends(require_permissions(CONFIG_EDITOR_PERMISSIONS))],
    summary="Check an edited config section",
    responses=response_docs(errors=(401, 403, 404)),
)
async def validate_config_section(request: ConfigValidateRequest) -> ConfigValidateResponse:
	"""Check an edited config section.

	Requires `robot:control` and `roles:write`. Merges `data` into the current file and runs every check a save
	and the next startup run. Each issue names the dotted field path it concerns, from the
	config root; this developer endpoint returns paths on purpose so the editor can mark the
	field. Writes nothing.
	"""
	section = _editable_section(request.section)
	document = get_config_store().document().data
	document[section] = request.data
	issues = _config_issues(document)
	return ConfigValidateResponse(valid=not issues, issues=issues)


@router.put(
    "/config",
    response_model=ConfigSaveResponse,
    dependencies=[Depends(require_permissions(CONFIG_EDITOR_PERMISSIONS))],
    summary="Save one config section and restart the API",
    responses=response_docs(errors=(401, 403, 404, 409, 422)),
)
async def save_config_section(request: ConfigSaveRequest) -> ConfigSaveResponse:
	"""Save one config section and restart the API.

	Requires `robot:control` and `roles:write`. Replaces the section in the config file when the file is still
	at `revision` (`409 config_changed` otherwise) and the result passes every startup check
	(`422 config_invalid`; call `POST /config/validate` for the field paths). The previous file
	is kept as a timestamped backup. Refuses with `409` while the robot moves, has queued work,
	or calibrates, and otherwise restarts the API after responding.
	"""
	section = _editable_section(request.section)
	await _ensure_safe_to_restart()

	document = get_config_store().document()
	if document.revision != request.revision:
		raise api_error(status.HTTP_409_CONFLICT, "config_changed", "")
	document.data[section] = request.data
	try:
		replace_root_config(document.data, expected_revision=request.revision)
	except ConfigConflictError as exc:
		raise api_error(status.HTTP_409_CONFLICT, "config_changed", "") from exc
	except (ConfigValidationError, ConfigRequirementError) as exc:
		raise api_error(status.HTTP_422_UNPROCESSABLE_ENTITY, "config_invalid", "") from exc

	logger.info("config_section_saved", section=section)
	_schedule_api_restart()
	return ConfigSaveResponse(revision=get_config_store().document().revision, restarting=True)
