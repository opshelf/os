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

# The text of the reported failure: the pool is exhausted, and every request that needs
# a connection waits out the 30 second pool timeout and then fails.
POOL_EXHAUSTED_MESSAGE = "QueuePool limit"


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

	`sqlalchemy.exc.TimeoutError` on its own also means a statement or a lock timeout,
	so the message is what identifies the pool running dry.
	"""
	current: BaseException | None = exc
	while current is not None:
		if isinstance(current, SQLAlchemyTimeoutError) and POOL_EXHAUSTED_MESSAGE in str(current):
			return True
		current = current.__cause__ or current.__context__
	return False


def _pool_status() -> dict[str, str]:
	"""The pool's own `status()` summary, for the log entries that report a leak.

	The reported failure reaches the platform as a cancellation with no mention of the
	pool, which is why the counts are logged here: a pool with connections checked out
	while nothing is running is leaking, and one that is merely busy is not. `status()`
	is used rather than the individual counters so nothing depends on pool internals.
	"""
	return {"pool_status": str(engine.sync_engine.pool.status())}


async def _rollback_or_ignore_closed_connection(session: AsyncSession) -> None:
	try:
		await session.rollback()
	except Exception as exc:
		if _is_inactive_sqlite_connection_error(exc):
			return
		raise


async def _close_or_ignore_closed_connection(session: AsyncSession) -> None:
	try:
		await session.close()
	except Exception as exc:
		if _is_inactive_sqlite_connection_error(exc):
			return
		raise


def _close_done(task: asyncio.Task[None]) -> None:
	_closing.discard(task)
	if task.cancelled():
		return
	exc = task.exception()
	if exc is not None:
		logger.error("Failed to close a database session", exc_info=exc, **_pool_status())


async def _close_session(session: AsyncSession) -> None:
	task = asyncio.ensure_future(_close_or_ignore_closed_connection(session))
	_closing.add(task)
	task.add_done_callback(_close_done)
	await asyncio.shield(task)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
	"""Get async database session."""
	session = AsyncSessionLocal()
	try:
		yield session
	except Exception as exc:
		if _is_pool_exhausted_error(exc):
			logger.error("database_connection_pool_exhausted", error=str(exc), **_pool_status())
		if session.in_transaction():
			await _rollback_or_ignore_closed_connection(session)
		raise
	finally:
		await _close_session(session)
