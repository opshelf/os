from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import sys

StatusPrinter = Callable[[str], None]


def _default_print_status(message: str) -> None:
	print(message, flush=True)


_status_printer: StatusPrinter = _default_print_status


def get_current_timestamp() -> str:
	return datetime.now(timezone.utc).isoformat()


def should_skip_motion_cleanup_on_cancel() -> bool:
	_, active_exception, _ = sys.exc_info()

	if isinstance(active_exception, asyncio.CancelledError) is True:
		return True

	current_task = asyncio.current_task()

	if current_task is None:
		return False

	return current_task.cancelling() > 0


def raise_if_cancelled(should_abort: Callable[[], bool] | None = None) -> None:
	if should_abort is not None and should_abort() is True:
		raise asyncio.CancelledError()

	current_task = asyncio.current_task()

	if current_task is not None and current_task.cancelling() > 0:
		raise asyncio.CancelledError()


def print_status(message: str) -> None:
	_status_printer(message)


def set_status_printer(status_printer: StatusPrinter | None) -> StatusPrinter:
	global _status_printer

	previous_status_printer = _status_printer
	_status_printer = _default_print_status if status_printer is None else status_printer
	return previous_status_printer
