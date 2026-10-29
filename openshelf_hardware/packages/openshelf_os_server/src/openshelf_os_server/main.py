from __future__ import annotations
from openshelf_os_server.documentation import HealthResponse, SystemInfoResponse, response_docs

import asyncio
import fcntl
import json
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
import logging
import socket
import struct
from pathlib import Path
from typing import Any, AsyncGenerator, NamedTuple, Optional, cast
from urllib.parse import urlsplit

import sentry_sdk
import uvicorn
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import FastAPI, Request
from openshelf_os_server.documentation import DESCRIPTION, TAGS, configure_security_documentation
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import HttpTransport
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.pool import NullPool
from starlette.exceptions import HTTPException as StarletteHTTPException

from openshelf_os.device import get_device_alias, get_device_id, get_release_branch, get_release_sha
from openshelf_os import (
    DOOR_MOTOR_RECOVERY_EVENT,
    _redact_sensitive_mapping,
    _sentry_event_has_door_motor_recovery_failure,
    _sentry_event_has_message,
    _sentry_event_has_suppressed_pymodbus_message,
    _sentry_event_is_simulated_motor_fault,
    _sentry_log_has_door_motor_recovery_failure,
    _sentry_log_has_message,
    _sentry_log_is_suppressed_pymodbus_message,
    _strip_pymodbus_frame_dump,
)
from openshelf_os.common import CONFIG_PATH, OperatingMode
from openshelf_os.operating_mode import is_sentry_reporting_enabled, is_service_process
from openshelf_os.runtime_state import RuntimeStatePaths
from openshelf_os_server.core.logging_config import configure_logging, get_logger, get_correlation_id
from openshelf_os_server.core.migrations import rename_module_side_values
from openshelf_os_server.core.robot import get_robot_health, init_robot_worker, stop_robot_worker
from openshelf_os_server.core.init_progress import get_init_tracker
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.bin_storage import BinStorageService
from openshelf_os_server.core.camera import (
	init_camera_progress,
	init_overhead_camera_manager,
	init_overview_camera_manager,
	init_tool_camera_manager,
	stop_all_camera_streams,
)
from openshelf_os_server.core.crash_video_recorder import (
	start_crash_video_recorder,
	stop_crash_video_recorder,
)
from openshelf_os_server.core.iobox_sizer import get_iobox_sizer_status, init_iobox_sizer, stop_iobox_sizer
from openshelf_os_server.core.mock_robot import (
	get_mock_robot_runtime,
	start_mock_robot_runtime,
	stop_mock_robot_runtime,
)
from openshelf_os_server.core.command_lifecycle import recover_interrupted_commands
from openshelf_os_server.errors import (
	capture_unhandled_exception,
	error_detail,
	internal_error_detail,
	register_mapped_handlers,
	request_correlation_id,
)
from openshelf_os_server.settings import Settings
from openshelf_os_server.database.base import Base, engine, get_database_url
from openshelf_os_server.database.session import AsyncSessionLocal, get_db
from openshelf_os_server.database.models import AccessLevel, Role, User
from openshelf_os_server.launcher import bootstrap_runtime_state
from openshelf_os_server.middleware.audit import AuditMiddleware
from openshelf_os_server.middleware.correlation import CorrelationIdMiddleware
from openshelf_os_server.middleware.inventory_events import InventoryEventsMiddleware
from openshelf_os_server.routers import access_levels, audit, auth, cabinet, calibration, camera, cycle_test, chemicals, demo, import_jobs, iobox_sizer, onboarding, printer, report, robot, roles, storage, system, telemetry, users, wifi
from openshelf_os_server.settings import (
	DEFAULT_DEVELOPER_PIN,
	DEVELOPER_SYSTEM_USER_EMAIL,
	configured_developer_pin,
	get_operating_mode,
	get_settings,
)
from openshelf_os_server.utils.cors_policy import allows_credentials, resolve_allowed_origins

configure_logging()
logger = get_logger(__name__)

PYMODBUS_REQUEST_ID_MISMATCH_PREFIX = "ERROR: request ask for id="
PYMODBUS_NO_RESPONSE_RETRY_PREFIX = "No response received after "
PYMODBUS_NO_RESPONSE_RETRY_SUFFIX = " retries, continue with next request"
# pymodbus' Log.error/Log.critical append the last frames as "\n>>>>> send: ..." /
# "\n>>>>> recv: ..." lines whenever DEBUG logging is off, so the message of a
# no-response retry is the log text plus that trailer. Every appended line
# carries the marker prefix; a deduplicated frame shows up as
# ">>>>> Repeating....".
# The same text is also what the hardware layer raises with, for a request the drive never
# answered (hardware.controller.ModbusTransientNoResponseError), so it is matched anywhere in the
# message and not only at its start; see the note on the same constants in openshelf_os.__init__.
ROBOT_INITIALIZATION_FAILED_EVENT = "robot_initialization_failed"
TOOL_FAULT_LOG_EVENT = "Tool fault detected - manual intervention required"
# The message uvicorn hands to Task.cancel() for every request task that is still running when
# the drain window given to it in start() (`timeout_graceful_shutdown`) expires. Its presence
# marks a cancellation uvicorn started itself rather than one a client or the app caused.
UVICORN_SHUTDOWN_CANCEL_MESSAGE = "Task cancelled, timeout graceful shutdown exceeded"
# Exceptions anyio leaves as the context of a cancellation delivered inside its own streams:
# MemoryObjectReceiveStream.receive() tries the non-blocking path first, catches the WouldBlock
# it raises, and only then awaits its receiver event, so a cancel that lands on that await carries
# WouldBlock as its context. Starlette's BaseHTTPMiddleware awaits such a stream in call_next.
ANYIO_STREAM_CONTEXT_ERRORS = (
	"WouldBlock",
	"EndOfStream",
	"ClosedResourceError",
	"BrokenResourceError",
)
# anyio names a task after the callable spawned into its task group, and records the repr of the
# task that called CancelScope.cancel() in the CancelledError message it delivers. These are the
# request tasks that tear a streaming request down once the client stops listening: sse-starlette
# cancels its response task group from cancel_on_finish, and Starlette's BaseHTTPMiddleware cancels
# the scope running the endpoint from call_next.<locals>.coro.
ANYIO_CANCEL_SCOPE_MESSAGE = "Cancelled via cancel scope"
ASGI_REQUEST_CANCEL_TASKS = (
	"cancel_on_finish",
	"starlette.middleware.base.BaseHTTPMiddleware",
)
DOOR_MOTOR_RECOVERY_FAILED_PREFIX = "Door motor recovery failed:"
# The log line uvicorn writes when the drain window given to it in start()
# (`timeout_graceful_shutdown`) expires. uvicorn.Server.shutdown logs it through its own
# "uvicorn.error" logger at ERROR level, with the number of requests still running as the only
# parameter, and then cancels those requests and exits. Stored as the two parts around that number so
# the unformatted record ("Cancel %s running task(s), ...") matches as well as the formatted one.
UVICORN_SHUTDOWN_DRAIN_PREFIX = "Cancel "
UVICORN_SHUTDOWN_DRAIN_SUFFIX = " running task(s), timeout graceful shutdown exceeded"
ROBOT_UNAVAILABLE_REQUEST_PATH_PREFIXES = (
	"/api/robot",
	"/api/calibration",
	"/api/demo",
	"/api/cabinet",
)



def _is_pymodbus_request_id_mismatch(message: Any) -> bool:
	return isinstance(message, str) is True and message.startswith(PYMODBUS_REQUEST_ID_MISMATCH_PREFIX)



def _is_pymodbus_no_response_retry(message: Any) -> bool:
	if isinstance(message, str) is False:
		return False

	log_text = _strip_pymodbus_frame_dump(message)

	start = log_text.find(PYMODBUS_NO_RESPONSE_RETRY_PREFIX)
	if start == -1 or log_text.endswith(PYMODBUS_NO_RESPONSE_RETRY_SUFFIX) is False:
		return False

	retry_count = log_text[
		start + len(PYMODBUS_NO_RESPONSE_RETRY_PREFIX) : -len(PYMODBUS_NO_RESPONSE_RETRY_SUFFIX)
	]
	return retry_count.isdigit()


def _is_suppressed_pymodbus_log_message(message: Any) -> bool:
	return (
		_is_pymodbus_request_id_mismatch(message) is True
		or _is_pymodbus_no_response_retry(message) is True
	)


def _is_uvicorn_shutdown_drain_message(message: Any) -> bool:
	if isinstance(message, str) is False:
		return False

	if message.startswith(UVICORN_SHUTDOWN_DRAIN_PREFIX) is False:
		return False

	return message.endswith(UVICORN_SHUTDOWN_DRAIN_SUFFIX) is True


def _is_door_motor_recovery_failure(message: Any) -> bool:
	return isinstance(message, str) is True and message.startswith(DOOR_MOTOR_RECOVERY_FAILED_PREFIX)



def _sentry_log_is_uvicorn_shutdown_drain(log: dict[str, Any]) -> bool:
	for key in ("body", "message"):
		if _is_uvicorn_shutdown_drain_message(log.get(key)) is True:
			return True

	attributes_value = log.get("attributes")
	if isinstance(attributes_value, dict) is False:
		return False

	attributes = cast(dict[str, Any], attributes_value)
	return any(_is_uvicorn_shutdown_drain_message(value) is True for value in attributes.values())




def _sentry_event_exception_values(event: dict[str, Any]) -> list[dict[str, Any]]:
	exception_value = event.get("exception")
	if isinstance(exception_value, dict) is False:
		return []

	exception_data = cast(dict[str, Any], exception_value)
	values = exception_data.get("values")
	if not isinstance(values, list):
		return []

	values_list = cast(list[Any], values)
	return [value for value in values_list if isinstance(value, dict) is True]





def _sentry_event_is_uvicorn_shutdown_drain(event: dict[str, Any]) -> bool:
	"""Drop the notice uvicorn logs when its shutdown drain window expires.

	Long-lived requests - an SSE status or event stream, an MJPEG camera stream - never finish on
	their own, so the window start() gives uvicorn expires on every restart with a live client and
	uvicorn.Server.shutdown logs the drain message at ERROR level before cancelling those requests and
	exiting. The LoggingIntegration turns any ERROR record into an issue, so each planned restart filed
	a stackless log-only event here next to the CancelledError the cancellation raises in the request.
	Cancelling the requests that outlived the window is the configured outcome of a restart, not a
	fault, and the record carries no stack, no request and no device state, so it is dropped. Both the
	formatted message and the "%s" template uvicorn passed to the logger are matched.
	"""
	if _is_uvicorn_shutdown_drain_message(event.get("message")) is True:
		return True

	logentry_value = event.get("logentry")
	if isinstance(logentry_value, dict) is True:
		logentry = cast(dict[str, Any], logentry_value)
		if any(_is_uvicorn_shutdown_drain_message(logentry.get(key)) is True for key in ("message", "formatted")):
			return True

	entries_value = event.get("entries", [])
	entries = cast(list[Any], entries_value) if isinstance(entries_value, list) is True else []
	for entry in entries:
		if isinstance(entry, dict) is False:
			continue

		data = entry.get("data")
		if isinstance(data, dict) is False:
			continue

		if any(_is_uvicorn_shutdown_drain_message(data.get(key)) is True for key in ("message", "formatted")):
			return True

	return False


def _sentry_event_is_asgi_request_cancel(event: dict[str, Any]) -> bool:
	"""Drop a cancellation an ASGI request task issued while nothing but a cancellation failed.

	A streaming endpoint such as /api/robot/events is torn down by cancelling its request task:
	sse-starlette's `cancel_on_finish` cancels the response task group when the response finishes,
	and Starlette's BaseHTTPMiddleware cancels the scope running the endpoint in
	`call_next.<locals>.coro`. The CancelledError then surfaces from whatever the teardown was
	awaiting at that moment, so a pooled SQLite connection being returned is one of the await
	points SQLAlchemy's pool logs (`Exception terminating connection`) with the cancellation
	attached. Those are the configured outcome of a client that stopped listening or of a restart
	that cancelled the request, not a fault.

	The cancelling task is the request task, and a real failure can be chained to the cancellation,
	so both stay reportable: the event is dropped only when every serialized exception is a
	cancellation, or one of anyio's stream bookkeeping types, and one of them names an ASGI request
	task as the canceller.
	"""
	values = _sentry_event_exception_values(event)
	if len(values) == 0:
		return False

	is_request_cancel = False
	for value in values:
		if _sentry_exception_type_is_one_of(value, ("CancelledError",)) is True:
			exception_message_value = value.get("value")
			if not isinstance(exception_message_value, str):
				continue

			exception_message = exception_message_value
			if ANYIO_CANCEL_SCOPE_MESSAGE not in exception_message:
				continue

			if any(task in exception_message for task in ASGI_REQUEST_CANCEL_TASKS) is True:
				is_request_cancel = True

			continue

		if _sentry_exception_type_is_one_of(value, ANYIO_STREAM_CONTEXT_ERRORS) is True:
			continue

		return False

	return is_request_cancel


def _sentry_event_is_http_exception(event: dict[str, Any], hint: dict[str, Any]) -> bool:
	original_exception = hint.get("original_exception")
	if isinstance(original_exception, StarletteHTTPException) is True:
		return True

	for value in _sentry_event_exception_values(event):
		exception_type = value.get("type")
		if not isinstance(exception_type, str):
			continue

		if exception_type == "HTTPException" or exception_type.endswith(".HTTPException") is True:
			return True

	return False


def _request_path_matches_prefix(request_url: str, prefixes: tuple[str, ...]) -> bool:
	if request_url == "":
		return False

	path = urlsplit(request_url).path
	return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def _sentry_event_is_robot_unavailable_request(event: dict[str, Any]) -> bool:
	request_value = event.get("request")
	request_url = ""
	if isinstance(request_value, dict) is True:
		request = cast(dict[str, Any], request_value)
		url_value = request.get("url")
		if isinstance(url_value, str) is True:
			request_url = str(url_value)

	is_robot_unavailable_request = _request_path_matches_prefix(
		request_url,
		ROBOT_UNAVAILABLE_REQUEST_PATH_PREFIXES,
	)
	if is_robot_unavailable_request is True and _sentry_event_has_message(event, "robot_unavailable") is True:
		return True

	for value in _sentry_event_exception_values(event):
		exception_message_value = value.get("value")
		if not isinstance(exception_message_value, str):
			continue

		exception_message = exception_message_value
		if "Robot is not initialized or failed to start" in exception_message:
			return True

		if is_robot_unavailable_request is False:
			continue

		if "Robot Worker was Not Initialized" in exception_message:
			return True

		if "Robot is not initialized" in exception_message:
			return True

	return False


def _sentry_exception_type_is_one_of(value: dict[str, Any], names: tuple[str, ...]) -> bool:
	exception_type_value = value.get("type")
	if not isinstance(exception_type_value, str):
		return False

	exception_type = exception_type_value
	for name in names:
		if exception_type == name or exception_type.endswith(f".{name}"):
			return True

	return False


def _sentry_event_is_shutdown_cancelled_request(event: dict[str, Any]) -> bool:
	"""Drop a request uvicorn cancelled when its shutdown drain window expired.

	start() gives uvicorn a bounded graceful shutdown so an API restart exits even while a client
	still holds a long-lived request - an SSE status or event stream, an MJPEG camera stream, or a
	move that runs longer than the window. When the window expires, uvicorn cancels that request task
	itself and the CancelledError surfaces from whatever the ASGI stack was sitting on, so the frames
	are framework bookkeeping and the event says only "this request was still running at restart".

	That is the configured outcome of a restart, not a fault, so it is dropped. The cancellation is
	accepted on its own, though: a real failure that was chained to it stays reportable, so any
	exception type that is not a cancellation or one of anyio's stream bookkeeping types keeps the
	event.
	"""
	values = _sentry_event_exception_values(event)
	if len(values) == 0:
		return False

	is_shutdown_cancel = False
	for value in values:
		if _sentry_exception_type_is_one_of(value, ("CancelledError",)) is True:
			exception_message_value = value.get("value")
			if not isinstance(exception_message_value, str):
				continue

			exception_message = exception_message_value
			if UVICORN_SHUTDOWN_CANCEL_MESSAGE in exception_message:
				is_shutdown_cancel = True

			continue

		if _sentry_exception_type_is_one_of(value, ANYIO_STREAM_CONTEXT_ERRORS) is True:
			continue

		return False

	return is_shutdown_cancel


def _before_send_sentry_event(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	if is_sentry_reporting_enabled() is False:
		return None

	if _sentry_event_is_http_exception(event, _hint) is True:
		return None

	if _sentry_event_is_asgi_request_cancel(event) is True:
		return None

	if _sentry_event_has_suppressed_pymodbus_message(event) is True:
		return None

	if _sentry_event_is_uvicorn_shutdown_drain(event) is True:
		return None

	if _sentry_event_has_message(event, DOOR_MOTOR_RECOVERY_EVENT) is True:
		return None

	# The IO-box door recovery is a best-effort self-healing path that keeps the
	# cached door state when its recovery home fails; the motor fault itself is
	# still reported separately as the motor's "Fault detected - stopping motor"
	# warning or an ERROR-level motion-timeout event. The recovery message is
	# stackless, so drop the whole recovery-failure class instead of a log-only
	# issue grouped by that message.
	if _sentry_event_has_door_motor_recovery_failure(event) is True:
		return None

	if _sentry_event_is_robot_unavailable_request(event) is True:
		return None

	if _sentry_event_is_shutdown_cancelled_request(event) is True:
		return None

	# The debug fault-injection endpoint (POST /api/robot/debug/motors/{motor}/crash) drives
	# the real crash pipeline on purpose - crash state, needs-home, artifacts - but the fault
	# it raises is synthetic: it is not an incident and has no meaningful stack.
	if _sentry_event_is_simulated_motor_fault(event) is True:
		return None

	request_value = event.get("request")
	if isinstance(request_value, dict) is True:
		request = cast(dict[str, Any], request_value)
		headers_value = request.get("headers")
		if isinstance(headers_value, dict) is True:
			request["headers"] = _redact_sensitive_mapping(headers_value)

	return event


def _before_send_sentry_log(log: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
	if is_sentry_reporting_enabled() is False:
		return None

	if _sentry_log_is_suppressed_pymodbus_message(log) is True:
		return None

	if _sentry_log_is_uvicorn_shutdown_drain(log) is True:
		return None

	# The startup path already captures the underlying exception separately with a stacktrace.
	# Dropping the structured log avoids a duplicate log-only issue grouped by message.
	if _sentry_log_has_message(log, ROBOT_INITIALIZATION_FAILED_EVENT) is True:
		return None

	# Tool faults are also reported by the robot worker with the original exception,
	# stacktrace, command context, and motor diagnostics. The warning log is useful
	# locally, but sending it to Sentry creates a second log-only issue with no stack.
	if _sentry_log_has_message(log, TOOL_FAULT_LOG_EVENT) is True:
		return None

	if _sentry_log_has_message(log, DOOR_MOTOR_RECOVERY_EVENT) is True:
		return None

	if _sentry_log_has_door_motor_recovery_failure(log) is True:
		return None

	attributes_value = log.get("attributes")
	if isinstance(attributes_value, dict) is False:
		return log

	log["attributes"] = _redact_sensitive_mapping(attributes_value)

	return log

device_id = get_device_id()

sentry_enabled = False


def _configure_sentry() -> bool:
	sentry_dsn = os.getenv("SENTRY_DSN_API", "")
	mode = get_operating_mode()

	# Initialized in every mode so the runtime toggle can turn reporting on; the
	# before_send hooks drop everything while it is off.
	if is_service_process() is False:
		logger.info("sentry_disabled_outside_service", mode=mode.value)
		return False

	if sentry_dsn == "":
		logger.warning("sentry_not_configured", reason="SENTRY_DSN_API not set", mode=mode.value)
		return False

	# Errors only: each one is sent the moment it is captured. Sentry logs, traces, and
	# release-health sessions are batched or sent at exit, so a shutdown always found
	# some pending and waited out shutdown_timeout.
	sentry_sdk.init(
		dsn=sentry_dsn,
		enable_logs=False,
		auto_session_tracking=False,
		integrations=[
			StarletteIntegration(transaction_style="endpoint"),
			FastApiIntegration(transaction_style="endpoint"),
			LoggingIntegration(
				sentry_logs_level=logging.INFO,
				level=logging.INFO,
				event_level=logging.ERROR,
			),
		],
		traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0")),
		environment=mode.value,
		release=os.getenv("APP_VERSION", "0.1.0"),
		server_name=device_id,
		before_send=cast(Any, _before_send_sentry_event),
		before_send_log=cast(Any, _before_send_sentry_log),
		transport=HttpTransport,
		shutdown_timeout=2,
		debug=os.getenv("SENTRY_DEBUG", "false").lower() in ("true", "1", "yes"),
	)
	sentry_sdk.set_tag("device_id", device_id)
	sentry_sdk.set_context("opshelf_device", {"id": device_id})
	logger.info("sentry_initialized", dsn_configured=True, device_id=device_id, mode=mode.value)
	return True


sentry_enabled = _configure_sentry()


MISSING_SCHEMA_DETAIL_LIMIT = 25


class _SchemaState(NamedTuple):
	"""What the live database holds compared to the schema the models describe."""

	current_revisions: list[str]
	tables_present: int
	missing: list[str]


def _alembic_config(database_url: str) -> Optional[Config]:
	script_location = Path(__file__).parent / "alembic"

	if script_location.exists() is False:
		logger.warning("alembic_directory_missing", script_location=str(script_location))
		return None

	# Historical resolution, kept as is: the packaged alembic.ini is not found from
	# here, so the fallback Config is what runs in the image. Loading that ini would
	# make alembic/env.py call fileConfig(), which disables the app loggers that
	# configure_logging() already installed.
	alembic_ini_path = Path(__file__).parent.parent.parent.parent.parent / "alembic.ini"

	if alembic_ini_path.exists() is False:
		logger.warning("alembic_ini_missing", path=str(alembic_ini_path), action="using_fallback_config")
		alembic_cfg = Config()
	else:
		alembic_cfg = Config(str(alembic_ini_path))

	alembic_cfg.set_main_option("script_location", str(script_location))
	alembic_cfg.set_main_option("sqlalchemy.url", database_url)
	return alembic_cfg


def _schema_state(database_url: str) -> _SchemaState:
	"""Compare the live schema with the schema `Base.metadata` describes.

	The API builds its databases with `Base.metadata.create_all`, and a replay that
	dies partway leaves behind the `alembic_version` rows sqlite committed around its
	DDL, so neither the revision row nor its absence says where the schema really is.
	Missing lists every model table, and every `table.column`, that the database does
	not have yet.
	"""
	sync_engine = create_engine(database_url, poolclass=NullPool)

	try:
		with sync_engine.connect() as connection:
			inspector = inspect(connection)
			table_names = set(inspector.get_table_names())
			current_revisions: list[str] = []

			if "alembic_version" in table_names:
				current_revisions = sorted(
					str(row[0]) for row in connection.exec_driver_sql("SELECT version_num FROM alembic_version")
				)

			missing: list[str] = []

			for table_name, table in Base.metadata.tables.items():
				if table_name not in table_names:
					missing.append(table_name)
					continue

				columns = {column["name"] for column in inspector.get_columns(table_name)}
				missing.extend(
					f"{table_name}.{column.name}"
					for column in table.columns
					if column.name not in columns
				)

			missing_tables = len([entry for entry in missing if "." not in entry])
			return _SchemaState(
				current_revisions=current_revisions,
				tables_present=len(Base.metadata.tables) - missing_tables,
				missing=missing,
			)
	finally:
		sync_engine.dispose()


def _apply_module_side_rename(database_url: str) -> None:
	"""Re-run the idempotent side-value rename that a baseline stamp skips.

	`_schema_state` compares table and column names only, so the data-only
	revision f9a0b1c2d3e4 (left/right to front/back in `item_locations` and
	`storage_bins`) is invisible to it: a field database stamped onto head
	would keep its old side values under code that no longer matches them.
	The UPDATEs only touch rows that still carry the old values, so re-running
	them after every startup path is a no-op in steady state.
	"""
	sync_engine = create_engine(database_url, poolclass=NullPool)

	try:
		with sync_engine.begin() as connection:
			rename_module_side_values(connection, forward=True)
	finally:
		sync_engine.dispose()


def _run_migrations() -> None:
	"""Bring the database to revision head, or baseline it when it is already there.

	Databases in the field were built by `Base.metadata.create_all`, not by this
	revision chain: they already carry the schema head describes while
	`alembic_version` holds no version, or an old row an aborted replay left behind.
	Replaying the full history into them can never complete - revisions create tables
	and indexes that are already there (`storage_bins`,
	`ix_item_sizer_measurements_upca`), and no revision creates `items`,
	`item_locations` or `storage_cells`, whose absence breaks the batch rebuild in
	a4b5c6d7e8f9. Every abort logged `migration_failed`, and since sqlite had already
	committed the version rows around its DDL the next start replayed the same way, so
	every restart of every device raised this alert again.

	A database whose schema matches the models - or that holds no model table at all
	and is about to get them from the create_all that runs right after here - is
	baselined with a stamp instead. A stamp writes one `alembic_version` row, touches
	no rows and no tables, and revisions land incrementally from head from then on.
	"""
	try:
		db_url = get_database_url()
		alembic_cfg = _alembic_config(db_url)

		if alembic_cfg is None:
			return

		heads = sorted(ScriptDirectory.from_config(alembic_cfg).get_heads())
		state = _schema_state(db_url)
		fresh_database = state.tables_present == 0
		schema_is_current = state.missing == []

		if state.current_revisions != heads and (fresh_database is True or schema_is_current is True):
			logger.info(
				"migrations_baseline_stamped",
				reason="empty_database" if fresh_database is True else "schema_matches_models",
				previous_revision=",".join(state.current_revisions) or None,
				tables_present=state.tables_present,
				model_tables=len(Base.metadata.tables),
			)
			command.stamp(alembic_cfg, "head")
			_apply_module_side_rename(db_url)
			logger.info("database_migrations_complete", action="baseline_stamp")
			return

		if fresh_database is False and schema_is_current is False:
			# The schema matches neither the models nor head, so the replay below is the
			# only path left. Name the drift it has to close: revisions cannot add a
			# column to a table they did not create, and an abort that follows has to say
			# what the device is short of.
			logger.warning(
				"migrations_history_replay",
				from_revision=",".join(state.current_revisions) or None,
				missing_count=len(state.missing),
				missing=state.missing[:MISSING_SCHEMA_DETAIL_LIMIT],
			)

		logger.info("running_database_migrations", script_location=str(Path(__file__).parent / "alembic"))
		command.upgrade(alembic_cfg, "head")
		_apply_module_side_rename(db_url)
		logger.info("database_migrations_complete")
	except Exception as e:
		logger.error("migration_failed", error=str(e), exc_info=True)
		logger.warning("continuing_without_migrations")


def _get_tool_camera_url(settings: Settings) -> str | None:
	if settings.tool_camera.enabled is False:
		return None

	if settings.tool_camera.rtsp_url != "":
		return settings.tool_camera.rtsp_url

	config_paths = [CONFIG_PATH]

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


def _get_overview_camera_urls(settings: Settings) -> dict[str, str]:
	cameras = {
		"overview_one": settings.overview_one_camera,
		"overview_two": settings.overview_two_camera,
	}
	return {
		camera_id: camera.rtsp_url
		for camera_id, camera in cameras.items()
		if camera.enabled is True and camera.rtsp_url != ""
	}


def _get_overview_camera_recording_urls(settings: Settings) -> dict[str, str]:
	cameras = {
		"overview_one": settings.overview_one_camera,
		"overview_two": settings.overview_two_camera,
	}
	return {
		camera_id: camera.recording_url
		for camera_id, camera in cameras.items()
		if camera.enabled is True and camera.rtsp_url != ""
	}


DEFAULT_ACCESS_LEVELS = ["Level 1", "Level 2", "Level 3"]

DEFAULT_ROLES: dict[str, list[str]] = {
	"admin": [
		"users:read", "users:write", "users:delete",
		"roles:read", "roles:write", "roles:delete",
		"robot:control",
		"storage:read", "storage:write", "storage:delete", "storage:access",
		"printer:print",
		"audit:read",
	],
	"operator": [
		"robot:control",
		"storage:read",
	],
	"viewer": [
		"storage:read",
	],
}


def _pin_matches(ph: PasswordHasher, pin_hash: str, pin: str) -> bool:
	try:
		return ph.verify(pin_hash, pin)
	except (VerifyMismatchError, InvalidHashError):
		return False


def _developer_pin_hash(ph: PasswordHasher, pin: Optional[str]) -> str:
	# Without a known PIN the user is locked with a random one; the relay identity
	# and the system-user filters still rely on the row existing.
	return ph.hash(pin if pin is not None else secrets.token_urlsafe(32))


def _developer_pin_source(pin: Optional[str]) -> str:
	if pin is None:
		return "random"
	return "default" if pin == DEFAULT_DEVELOPER_PIN else "configured"


async def _ensure_developer_user() -> None:
	"""Recreate the developer system user on every startup, including after a
	database wipe, so the device can never end up fully locked out, and keep its
	PIN on the one the mode calls for (see `configured_developer_pin`).

	A production or demo device without a configured PIN must not keep the published
	default, so a hash that still verifies against it is replaced with a random one;
	the released migration that seeded it stays untouched.
	"""
	desired_pin = configured_developer_pin(get_operating_mode())
	ph = PasswordHasher()

	async with AsyncSessionLocal() as db:
		result = await db.execute(
			select(User).filter(User.email == DEVELOPER_SYSTEM_USER_EMAIL)
		)
		existing_user = result.scalar_one_or_none()

		if existing_user is not None:
			changed = False
			if existing_user.is_system is False:
				existing_user.is_system = True
				changed = True
				logger.info("developer_user_marked_system", email=DEVELOPER_SYSTEM_USER_EMAIL)
			if desired_pin is not None:
				needs_pin = _pin_matches(ph, existing_user.pin_hash, desired_pin) is False
			else:
				needs_pin = _pin_matches(ph, existing_user.pin_hash, DEFAULT_DEVELOPER_PIN) is True
			if needs_pin is True:
				existing_user.pin_hash = _developer_pin_hash(ph, desired_pin)
				changed = True
				logger.info(
					"developer_user_pin_reset",
					email=DEVELOPER_SYSTEM_USER_EMAIL,
					pin_source=_developer_pin_source(desired_pin),
				)
			if changed is True:
				await db.commit()
			logger.info("developer_user_exists", email=DEVELOPER_SYSTEM_USER_EMAIL)
			return

		role_result = await db.execute(select(Role).filter(Role.name == "admin"))
		admin_role = role_result.scalar_one_or_none()

		if admin_role is None:
			logger.warning("admin_role_not_found", action="skipping_developer_user_creation")
			return

		pin_hash = _developer_pin_hash(ph, desired_pin)

		developer_user = User(
			email=DEVELOPER_SYSTEM_USER_EMAIL,
			first_name="Developer",
			last_name="System",
			pin_hash=pin_hash,
			role_id=admin_role.id,
			is_active=True,
			is_system=True,
		)

		db.add(developer_user)
		await db.commit()
		logger.info("developer_user_created", email=DEVELOPER_SYSTEM_USER_EMAIL)


async def _dev_auto_user_is_resolvable(email: str) -> bool:
	"""Report whether the dev-mode fallback identity resolves to an active user.

	A miss is not fatal at startup, but every unauthenticated request then
	answers 503 with "Dev auto-user not found in database".
	"""
	async with AsyncSessionLocal() as db:
		result = await db.execute(
			select(User).filter(User.email == email, User.is_active.is_(True))
		)
		return result.scalar_one_or_none() is not None


async def _ensure_dev_auto_user(email: str) -> None:
	"""Create the configured dev auto user so the unauthenticated fallback
	resolves on any database (e.g. a fresh one created on first start in this
	mode). Mirrors _ensure_developer_user in development: admin role, PIN 1111, is_system.
	An existing inactive user is activated instead of duplicated."""
	if await _dev_auto_user_is_resolvable(email) is True:
		return

	async with AsyncSessionLocal() as db:
		result = await db.execute(select(User).filter(User.email == email))
		existing_user = result.scalar_one_or_none()

		if existing_user is not None:
			if existing_user.is_active is False:
				existing_user.is_active = True
				await db.commit()
				logger.info("dev_auto_user_activated", email=email)
			return

		role_result = await db.execute(select(Role).filter(Role.name == "admin"))
		admin_role = role_result.scalar_one_or_none()

		if admin_role is None:
			logger.warning("admin_role_not_found", action="skipping_dev_auto_user_creation")
			return

		ph = PasswordHasher()
		pin_hash = ph.hash(DEFAULT_DEVELOPER_PIN)
		local_part = email.partition("@")[0]

		db.add(
			User(
				email=email,
				first_name=local_part.capitalize(),
				last_name="User",
				pin_hash=pin_hash,
				role_id=admin_role.id,
				is_active=True,
				is_system=True,
			)
		)
		await db.commit()
		logger.info("dev_auto_user_created", email=email)


async def _ensure_default_roles_and_access_levels() -> None:
	async with AsyncSessionLocal() as db:
		for role_name, permissions in DEFAULT_ROLES.items():
			result = await db.execute(select(Role).filter(Role.name == role_name))
			if result.scalar_one_or_none() is None:
				db.add(Role(name=role_name, permissions=permissions))
				logger.info("default_role_created", role=role_name)

		for level_name in DEFAULT_ACCESS_LEVELS:
			level_result = await db.execute(select(AccessLevel).filter(AccessLevel.name == level_name))
			if level_result.scalar_one_or_none() is None:
				db.add(AccessLevel(name=level_name))
				logger.info("default_access_level_created", access_level=level_name)

		await db.commit()


async def _ensure_default_role_permissions() -> None:
	async with AsyncSessionLocal() as db:
		result = await db.execute(select(Role).filter(Role.name == "admin"))
		admin_role = result.scalar_one_or_none()

		if admin_role is None:
			return

		permissions = list(admin_role.permissions or [])
		if "*" in permissions or "storage:access" in permissions:
			return

		permissions.append("storage:access")
		admin_role.permissions = permissions
		await db.commit()
		logger.info("admin_role_permission_added", permission="storage:access")


LAST_MODE_FILE = RuntimeStatePaths.current().data_dir / ".last_mode"
DEMO_SEEDED_FILE = RuntimeStatePaths.current().data_dir / ".demo_workspace_seeded"


def _run_demo_seed() -> bool:
	script_path = Path(__file__).parent.parent.parent / "scripts" / "seed_demo.py"
	if script_path.exists() is False:
		logger.warning("demo_seed_script_not_found", script_path=str(script_path))
		return False

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
		return True

	logger.error("demo_seed_failed", returncode=result.returncode, stderr=result.stderr.strip())
	return False


async def _apply_mode_transition() -> None:
	mode = get_operating_mode()

	last_mode = None
	if LAST_MODE_FILE.exists() is True:
		raw = LAST_MODE_FILE.read_text(encoding="utf-8").strip().lower()
		if raw in {m.value for m in OperatingMode}:
			last_mode = raw

	if last_mode == mode.value and not (mode == OperatingMode.demo and DEMO_SEEDED_FILE.exists() is False):
		return

	logger.info("mode_transition", previous=last_mode, current=mode.value)

	if mode == OperatingMode.demo and DEMO_SEEDED_FILE.exists() is False:
		if _run_demo_seed() is False:
			return
		DEMO_SEEDED_FILE.write_text("seeded\n", encoding="utf-8")

	LAST_MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
	LAST_MODE_FILE.write_text(mode.value, encoding="utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
	runtime_paths = RuntimeStatePaths.current()
	logger.info(
		"application_starting",
		app_title=app.title,
		mode=runtime_paths.mode.value,
		config_path=str(runtime_paths.config_path),
		database_path=str(runtime_paths.database_path) if os.getenv("DATABASE_URL") is None else None,
		database_url_override=os.getenv("DATABASE_URL") is not None,
		calibration_dir=str(runtime_paths.calibration_dir),
	)
	settings = get_settings()
	is_mock = runtime_paths.mode == OperatingMode.mock

	_run_migrations()

	logger.info("initializing_database_schema")
	async with engine.begin() as conn:
		await conn.run_sync(Base.metadata.create_all)

	await _ensure_default_roles_and_access_levels()
	await _ensure_default_role_permissions()
	await _ensure_developer_user()
	if settings.auth.dev_mode and settings.auth.dev_auto_user:
		await _ensure_dev_auto_user(settings.auth.dev_auto_user)
		if await _dev_auto_user_is_resolvable(settings.auth.dev_auto_user) is False:
			logger.warning(
				"dev_auto_user_unresolvable",
				email=settings.auth.dev_auto_user,
				impact="unauthenticated requests answer 503 Dev auto-user not found in database",
				expected=DEVELOPER_SYSTEM_USER_EMAIL,
			)
	async with AsyncSessionLocal() as db:
		await onboarding.ensure_onboarding_state(db)
	await _apply_mode_transition()
	interrupted_commands = await recover_interrupted_commands()
	if interrupted_commands:
		logger.warning("robot_commands_recovered", count=interrupted_commands)

	# Configured bin identities and homes must be durable before the app can
	# serve requests. Interrupted motion is recovered here as well, before any
	# runtime mode can serve requests or initialize hardware.
	async with AsyncSessionLocal() as session:
		bin_storage = BinStorageService(session)
		await bin_storage.sync_config()
		await bin_storage.recover_interrupted_workflows()

	robot_init_task: Optional[asyncio.Task[None]] = None
	camera_init_task: Optional[asyncio.Task[None]] = None
	storage_override_set = False
	get_init_tracker().reset()
	if is_mock:
		await start_mock_robot_runtime()
		get_init_tracker().register_components(["mock_robot"])
		get_init_tracker().report("mock_robot", "ready")
		get_init_tracker().set_overall("ready")
		logger.info("mock_robot_initialized")
	elif settings.robot.enabled is True:
		logger.info("robot_initialization", status="starting_background")
		robot_init_task = asyncio.create_task(init_robot_worker())
	else:
		logger.info("robot_disabled", reason="settings_configuration")
		get_init_tracker().set_overall("disabled")

	camera_ids: list[str] = []

	tool_camera_url = None if is_mock else _get_tool_camera_url(settings)
	if tool_camera_url is not None and tool_camera_url != "":
		tool_recording_url = settings.tool_camera.recording_url
		try:
			await init_tool_camera_manager(tool_camera_url, tool_recording_url)
			camera_ids.append("tool")
			logger.info("tool_camera_initialized", rtsp_url=tool_camera_url)
		except Exception as e:
			logger.error("tool_camera_initialization_failed", error=str(e), exc_info=True)

	overhead_camera_url = None if is_mock else _get_overhead_camera_url(settings)
	if overhead_camera_url is not None and overhead_camera_url != "":
		overhead_recording_url = settings.overhead_camera.recording_url
		try:
			await init_overhead_camera_manager(overhead_camera_url, overhead_recording_url)
			camera_ids.append("overhead")
			logger.info("overhead_camera_initialized", rtsp_url=overhead_camera_url)
		except Exception as e:
			logger.error("overhead_camera_initialization_failed", error=str(e), exc_info=True)

	overview_camera_urls = {} if is_mock else _get_overview_camera_urls(settings)
	for camera_id, rtsp_url in overview_camera_urls.items():
		overview_recording_url = _get_overview_camera_recording_urls(settings)[camera_id]
		try:
			await init_overview_camera_manager(camera_id, rtsp_url, overview_recording_url)
			camera_ids.append(camera_id)
			logger.info("overview_camera_initialized", camera_id=camera_id, rtsp_url=rtsp_url)
		except Exception as e:
			logger.error("overview_camera_initialization_failed", camera_id=camera_id, error=str(e), exc_info=True)

	if is_mock is False:
		try:
			await init_iobox_sizer()
			if get_iobox_sizer_status()["enabled"] is True:
				camera_ids.append("iobox_sizer")
		except Exception as e:
			logger.warning("iobox_sizer_startup_failed", error=str(e), error_type=type(e).__name__, exc_info=True)

	if is_mock is False:
		await start_crash_video_recorder()

	if len(camera_ids) > 0:
		camera_init_task = asyncio.create_task(init_camera_progress(camera_ids))

	async def get_storage_session() -> AsyncGenerator[StorageService, None]:
		async for session in get_db():
			yield StorageService(session)

	app.dependency_overrides[StorageService] = get_storage_session
	storage_override_set = True

	try:
		yield
	finally:
		logger.info("application_shutting_down")
		try:
			await demo.shutdown_demo_cycle()
		except Exception as e:
			logger.warning("demo_cycle_shutdown_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
		if camera_init_task is not None and camera_init_task.done() is False:
			camera_init_task.cancel()
			try:
				await camera_init_task
			except asyncio.CancelledError:
				pass
			except Exception as e:
				logger.warning("camera_init_task_shutdown_failed", error=str(e), exc_info=True)
		try:
			await stop_crash_video_recorder()
		except Exception as e:
			logger.warning("crash_video_recorder_shutdown_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
		try:
			await stop_all_camera_streams()
		except Exception as e:
			logger.warning("camera_stream_shutdown_failed", error=str(e), error_type=type(e).__name__, exc_info=True)
		if robot_init_task is not None and robot_init_task.done() is False:
			robot_init_task.cancel()
			try:
				await robot_init_task
			except asyncio.CancelledError:
				pass
			except Exception as e:
				logger.warning("robot_init_task_shutdown_failed", error=str(e), exc_info=True)
		if is_mock:
			await stop_mock_robot_runtime()
			logger.info("mock_robot_stopped")
		else:
			await stop_robot_worker()
			logger.info("robot_worker_stopped")
			await stop_iobox_sizer()
		if storage_override_set is True:
			app.dependency_overrides.pop(StorageService, None)


app = FastAPI(
    lifespan=lifespan,
    title="Openshelf LB Local API",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# The UI is served from its own port, so any origin may call the API, but the API
# authenticates with bearer tokens and query tokens rather than cookies. A wildcard
# origin therefore never advertises credentialed access; operators who pin the origin
# list with OPENSHELF_CORS_ORIGINS get credentialed handling for those origins.
CORS_ALLOWED_ORIGINS = resolve_allowed_origins()

app.add_middleware(
	CORSMiddleware,
	allow_origins=CORS_ALLOWED_ORIGINS,
	allow_credentials=allows_credentials(CORS_ALLOWED_ORIGINS),
	allow_methods=["*"],
	allow_headers=["*"],
)
app.add_middleware(InventoryEventsMiddleware)
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(AuditMiddleware, sentry_enabled=sentry_enabled)
register_mapped_handlers(app)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
	correlation_id = get_correlation_id() or request_correlation_id(request)

	capture_unhandled_exception(exc, request.url.path, request.method, correlation_id, enabled=sentry_enabled)

	logger.error(
		"unhandled_exception",
		error=str(exc),
		error_type=type(exc).__name__,
		path=request.url.path,
		method=request.method,
		correlation_id=correlation_id,
		exc_info=True,
	)
	code, message = internal_error_detail(exc)
	detail: dict[str, str] = error_detail(message, code)
	if correlation_id:
		detail["correlation_id"] = correlation_id
	return JSONResponse(status_code=500, content={"detail": detail})


@app.get(
    "/api/health",
    tags=["system"],
    summary="Check OS and robot initialization",
    responses=response_docs(
        model=HealthResponse,
        existing={
            503: {
                "model": HealthResponse,
                "description": "Robot initialization failed or mock runtime unavailable",
            }
        },
    ),
)
async def health() -> JSONResponse:
	"""Check OS and robot initialization.

	Public health check. Reports the operating mode and robot initialization state. Returns
	`503` with a degraded body when initialization has failed, including when the mock
	runtime is unavailable. A healthy process may report a disabled robot; inspect
	`robot_status` and `robot_phase` before requesting motion.
	"""
	body: dict[str, str] = {"status": "ok", "mode": get_operating_mode().value}
	if get_operating_mode() == OperatingMode.mock:
		try:
			get_mock_robot_runtime()
		except ValueError:
			body["status"] = "degraded"
			body["robot_status"] = "error"
			body["robot_phase"] = "failed"
			return JSONResponse(status_code=503, content=body)
		body["robot_status"] = "ok"
		body["robot_phase"] = "ready"
		return JSONResponse(content=body)
	robot_health = get_robot_health()

	if robot_health["status"] == "error":
		body["status"] = "degraded"
		body["robot_status"] = "error"
		body["robot_error"] = str(robot_health.get("error", ""))
		body["robot_error_type"] = str(robot_health.get("error_type", "Error"))
		return JSONResponse(status_code=503, content=body)

	body["robot_status"] = str(robot_health["status"])
	body["robot_phase"] = str(robot_health.get("phase", ""))
	return JSONResponse(content=body)


@app.get(
    "/api/system/info",
    tags=["system"],
    summary="Read device identity and network address",
    responses=response_docs(model=SystemInfoResponse),
)
async def system_info() -> dict[str, str | None]:
	"""Read device identity, alias, release, and network address.

	Public device discovery information. `device_alias` is the operator-assigned alias
	staged by provisioning, or null when the device has none. `release_sha` and
	`release_branch` identify the release the device is running, staged by the
	on-device sync scripts, or null when unknown. `lan_ip` is the IPv4 address of
	`wlan0`, or null when that interface cannot be read. This endpoint does not
	establish robot readiness.
	"""
	lan_ip: str | None = None
	try:
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		lan_ip = socket.inet_ntoa(
			fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", b"wlan0"))[20:24]
		)
		s.close()
	except OSError:
		pass

	return {
		"device_id": get_device_id(),
		"device_alias": get_device_alias() or None,
		"release_sha": get_release_sha() or None,
		"release_branch": get_release_branch() or None,
		"lan_ip": lan_ip,
	}


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(access_levels.router)
app.include_router(audit.router)
app.include_router(camera.router, prefix="/api/camera")
app.include_router(iobox_sizer.router, prefix="/api/iobox/sizer")
app.include_router(cabinet.router, prefix="/api/cabinet")
app.include_router(robot.router, prefix="/api/robot")
app.include_router(storage.router, prefix="/api/storage")
app.include_router(import_jobs.router, prefix="/api/import-jobs")
app.include_router(chemicals.router, prefix="/api/chemicals")
app.include_router(printer.router, prefix="/api/print")
app.include_router(cycle_test.router)
app.include_router(calibration.router)
app.include_router(demo.router)
app.include_router(system.router, prefix="/api/system")
app.include_router(wifi.router, prefix="/api/wifi")
app.include_router(onboarding.router, prefix="/api/onboarding")
app.include_router(report.router, prefix="/api")
app.include_router(telemetry.router, prefix="/api")


configure_security_documentation(app)

def start() -> None:
	# Entry paths that call this directly, such as the development compose
	# command, must still fork the production runtime state before serving.
	bootstrap_runtime_state()

	log_config = uvicorn.config.LOGGING_CONFIG
	log_config["loggers"]["uvicorn.access"]["level"] = "ERROR"
	log_config["loggers"]["watchfiles"] = {"level": "ERROR"}

	reload_enabled = os.environ.get("UVICORN_RELOAD", "false").lower() == "true"
	reload_dirs = None
	reload_includes = None
	reload_excludes = None

	if reload_enabled is True:
		reload_dirs = [
			"/app/openshelf_hardware/packages/hardware/src/hardware",
			"/app/openshelf_hardware/packages/openshelf_os/src/openshelf_os",
			"/app/openshelf_hardware/packages/openshelf_os_server/src/openshelf_os_server",
		]
		reload_includes = ["*.py"]
		reload_excludes = ["*.json", "*.db", "*.db-shm", "*.db-wal"]

	uvicorn.run(
		"openshelf_os_server.main:app",
		host="0.0.0.0",
		port=8000,
		reload=reload_enabled,
		reload_dirs=reload_dirs,
		reload_includes=reload_includes,
		reload_excludes=reload_excludes,
		log_config=log_config,
		# Room for in-flight hardware operations (a rail power cycle holds power off for
		# 3s). Persistent streams end on the shutdown signal themselves (SSE through
		# sse-starlette, MJPEG through AppStatus), so the drain stops as soon as the
		# ordinary requests do.
		timeout_graceful_shutdown=5,
	)
