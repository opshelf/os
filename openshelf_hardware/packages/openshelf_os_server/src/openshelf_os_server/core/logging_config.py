"""Structured logging configuration for observability."""
import logging
import os
import sys
from contextvars import ContextVar
from typing import Any, MutableMapping, Mapping, cast

import structlog
from structlog.typing import WrappedLogger
from pythonjsonlogger.json import JsonFormatter

from openshelf_os.device import get_device_id

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
device_id_var: ContextVar[str | None] = ContextVar("device_id", default=None)
user_id_var: ContextVar[int | None] = ContextVar("user_id", default=None)
default_device_id = get_device_id()


class CustomJsonFormatter(JsonFormatter):
	"""Custom JSON formatter that adds context variables."""

	def add_fields(
		self,
		log_record: dict[str, Any],
		record: logging.LogRecord,
		message_dict: dict[str, Any]
	) -> None:
		super().add_fields(log_record, record, message_dict)
		log_record['service'] = 'api-server'
		log_record['environment'] = os.getenv('DEPLOYMENT_ENVIRONMENT', 'development')
		log_record['device_id'] = default_device_id

		correlation_id = correlation_id_var.get()
		if correlation_id is not None:
			log_record['correlation_id'] = correlation_id

		ctx_device_id = device_id_var.get()
		if ctx_device_id is not None:
			log_record['device_id'] = ctx_device_id

		user_id = user_id_var.get()
		if user_id is not None:
			log_record['user_id'] = user_id


def add_context_to_event_dict(
	logger: WrappedLogger,
	method_name: str,
	event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
	"""Add context variables to structlog event dict."""
	correlation_id = correlation_id_var.get()
	if correlation_id is not None:
		event_dict['correlation_id'] = correlation_id

	device_id = device_id_var.get()
	if device_id is not None:
		event_dict['device_id'] = device_id
	else:
		event_dict['device_id'] = default_device_id

	user_id = user_id_var.get()
	if user_id is not None:
		event_dict['user_id'] = user_id

	event_dict['service'] = 'api-server'
	event_dict['environment'] = os.getenv('DEPLOYMENT_ENVIRONMENT', 'development')

	return event_dict


def render_to_log_kwargs_preserve_event(
	logger: WrappedLogger,
	method_name: str,
	event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
	event = event_dict.get("event")
	log_kwargs = structlog.stdlib.render_to_log_kwargs(logger, method_name, event_dict)
	extra = log_kwargs.get("extra")

	if isinstance(extra, dict) is False or event is None:
		return log_kwargs

	extra_dict = cast(dict[str, Any], extra)

	if "event" not in extra_dict:
		extra_dict["event"] = event

	return log_kwargs


def configure_logging() -> None:
	"""Configure structured logging for the application."""
	log_level = os.getenv('LOG_LEVEL', 'DEBUG').upper()
	app_level = getattr(logging, log_level, logging.DEBUG)

	logging.basicConfig(
		level=app_level,
		handlers=[
			logging.StreamHandler(sys.stdout)
		]
	)

	for handler in logging.root.handlers:
		handler.setFormatter(
			CustomJsonFormatter(
				'%(asctime)s %(name)s %(levelname)s %(message)s',
				timestamp=True
			)
		)

	structlog.configure(
		processors=[
			structlog.contextvars.merge_contextvars,
			structlog.stdlib.filter_by_level,
			structlog.stdlib.add_logger_name,
			structlog.stdlib.add_log_level,
			structlog.stdlib.PositionalArgumentsFormatter(),
			structlog.processors.TimeStamper(fmt="iso"),
			structlog.processors.StackInfoRenderer(),
			structlog.processors.format_exc_info,
			structlog.processors.UnicodeDecoder(),
			add_context_to_event_dict,
			render_to_log_kwargs_preserve_event,
		],
		logger_factory=structlog.stdlib.LoggerFactory(),
		wrapper_class=structlog.stdlib.BoundLogger,
		cache_logger_on_first_use=True,
	)

	for name in ("openshelf_os_server", "openshelf_os", "hardware"):
		logging.getLogger(name).setLevel(app_level)

	third_party_warning = (
		"aiosqlite",
		"asyncio",
		"pymodbus",
		"serial",
		"serial_asyncio",
		"urllib3",
	)
	for name in third_party_warning:
		logging.getLogger(name).setLevel(logging.WARNING)

	logging.getLogger("uvicorn.access").setLevel(logging.ERROR)
	logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
	logging.getLogger("watchfiles.main").setLevel(logging.ERROR)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
	"""Get a structured logger instance."""
	logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
	return logger


def set_correlation_id(correlation_id: str) -> None:
	"""Set correlation ID for current context."""
	correlation_id_var.set(correlation_id)

def get_correlation_id() -> str | None:
    return correlation_id_var.get()


def clear_context() -> None:
	"""Clear all context variables."""
	correlation_id_var.set(None)
	device_id_var.set(None)
	user_id_var.set(None)
