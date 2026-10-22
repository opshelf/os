"""Database session management."""
import asyncio
import sqlite3
from typing import AsyncGenerator

from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .base import engine
from ..core.logging_config import get_logger

logger = get_logger(__name__)

AsyncSessionLocal = async_sessionmaker(
	engine, class_=AsyncSession, expire_on_commit=False
)

# A request cancelled while closing its session leaves the connection checked out: the
# cancellation interrupts the checkin, the aiosqlite worker thread stops, and the
# connection only returns when the garbage collector finalizes it, at an unbounded later
# time. Under BaseHTTPMiddleware anyio re-delivers the cancellation on every await, so any
# cleanup awaited in the request task is interrupted. The close therefore runs in a task
# of its own, held here until it finishes, and the request only waits on it through a
# shield.
_closing: set[asyncio.Task[None]] = set()


def _is_inactive_sqlite_connection_error(exc: BaseException) -> bool:
	"""Return True for cleanup failures from an already-closed aiosqlite connection."""
	current: BaseException | None = exc
	while current is not None:
		if isinstance(current, ValueError) and str(current) == "no active connection":
			return True
		if (
			isinstance(current, (sqlite3.OperationalError, SQLAlchemyOperationalError))
			and "no active connection" in str(current)
		):
			return True
		current = current.__cause__ or current.__context__
	return False


def _is_pool_exhausted_error(exc: BaseException) -> bool:
	"""Return True for the error raised when no pooled connection freed up in time.

	SQLAlchemy raises `sqlalchemy.exc.TimeoutError` only from the pool checkout.
	"""
	current: BaseException | None = exc
	while current is not None:
		if isinstance(current, SQLAlchemyTimeoutError):
			return True
		current = current.__cause__ or current.__context__
	return False


def _pool_status() -> dict[str, str]:
	return {"pool_status": str(engine.sync_engine.pool.status())}


async def _close_or_ignore_closed_connection(session: AsyncSession) -> None:
	try:
		await session.close()
	except Exception as exc:
		if _is_inactive_sqlite_connection_error(exc):
			return
		raise


def _log_close_failure(task: asyncio.Task[None]) -> None:
	if not task.cancelled() and (exc := task.exception()) is not None:
		logger.error("Failed to close a database session", exc_info=exc, **_pool_status())


async def _close_session(session: AsyncSession) -> None:
	task = asyncio.ensure_future(_close_or_ignore_closed_connection(session))
	_closing.add(task)
	task.add_done_callback(_closing.discard)
	try:
		await asyncio.shield(task)
	except asyncio.CancelledError:
		# The waiter is gone, so a close failure reaches nobody else.
		task.add_done_callback(_log_close_failure)
		raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
	"""Get async database session."""
	session = AsyncSessionLocal()
	try:
		yield session
	except Exception as exc:
		if _is_pool_exhausted_error(exc):
			# WARNING: the re-raised error is already reported; this adds the pool status.
			logger.warning("database_connection_pool_exhausted", error=str(exc), **_pool_status())
		raise
	finally:
		await _close_session(session)
