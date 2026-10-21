"""Database session management."""
import asyncio
import sqlite3
from collections.abc import Coroutine
from typing import Any, AsyncGenerator

from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .base import engine
from ..core.logging_config import get_logger

logger = get_logger(__name__)

AsyncSessionLocal = async_sessionmaker(
	engine, class_=AsyncSession, expire_on_commit=False
)

# A request cancelled at the worst moment keeps its connection out of the pool. The
# cancellation lands on the session's own database await, so a `session.close()` awaited
# from the cleanup is interrupted mid-checkin: the aiosqlite worker thread dies with the
# connection still checked out and SQLAlchemy can only drop it later under the garbage
# collector ("The garbage collector is trying to clean up non-checked-in connection ...,
# which will be dropped, as it cannot be safely terminated"). Nothing reclaims that
# connection, so the leak is permanent — 15 of them (the default pool size 5 plus its
# overflow of 10) and every later request of the process fails with
# "QueuePool limit of size 5 overflow 10 reached, connection timed out, timeout 30.00".
# Requests get cancelled routinely here: a client disconnecting from a slow route, a
# browser reloading with a request in flight, and every request still running when the
# bounded uvicorn shutdown drain (`timeout_graceful_shutdown`) expires — so a restart
# with the dev UI connected leaks about one connection per open request, and the
# streams the dev UI holds are the worst case.
#
# The cleanup therefore never awaits `session.close()` on the cancellation path.
# `asyncio.shield` hands the close to a task of its own, which the cancellation of the
# request does not reach, so the checkin finishes after the request is gone. The wait on
# that task is bounded below the 30 second pool timeout, so a close that never returns
# costs the request at most this much instead of the full timeout.
CLOSE_WAIT_LIMIT_S = 5.0

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


async def _run_uncancellable(coro: Coroutine[Any, Any, None]) -> None:
	"""Run a cleanup coroutine to completion even if the awaiting task is cancelled.

	`asyncio.shield` hands the coroutine to a task the outer cancellation does not reach
	but still re-raises `CancelledError` at the await, so the shield is waited on
	instead: that waits for the cleanup and hands back its outcome. The caller is already
	unwinding and only needs the connection back in the pool.

	The wait is bounded by `CLOSE_WAIT_LIMIT_S`. A close that never returns (a wedged
	aiosqlite worker, say) would otherwise hold the request task open, so past the limit
	the cleanup is left to its own task and the caller continues; that task still either
	finishes the checkin or leaves the connection to the pool's GC fallback.
	"""
	cleanup = asyncio.ensure_future(coro)
	shielded = asyncio.shield(cleanup)
	done, _ = await asyncio.wait([cleanup], timeout=CLOSE_WAIT_LIMIT_S)
	if not done:
		logger.warning(
			"database_session_close_stalled",
			timeout_s=CLOSE_WAIT_LIMIT_S,
			**_pool_status(),
		)
		return
	await shielded


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


async def _close_or_report_exhaustion(session: AsyncSession, context: str) -> None:
	"""Close a session, reporting the pool's state if the close timed out waiting for it.

	`session.close()` is where the reported "QueuePool limit … connection timed out,
	timeout 30.00" surfaces for a session whose connection was never handed back, so the
	failure has to be reported with the pool's own counters before it turns into the
	undifferentiated cancellation the platform shows.
	"""
	try:
		await _close_or_ignore_closed_connection(session)
	except Exception as exc:
		if _is_pool_exhausted_error(exc):
			logger.error(
				"database_connection_pool_exhausted",
				error=str(exc),
				context=context,
				**_pool_status(),
			)
		raise


async def _discard_session(session: AsyncSession) -> None:
	"""Hand a cancelled request's session back to the pool without ever raising.

	Returns once the connection is checked in, when the close fails for a real reason
	(that request is already unwinding with something more worth reporting, and the
	pool's GC fallback applies to the connection), or when the bounded wait runs out.
	"""
	try:
		await _run_uncancellable(_close_or_report_exhaustion(session, "cancelled request"))
	except asyncio.CancelledError:
		# Cannot happen while the wait is bounded, and nothing useful could be done
		# with it here, but it must not vanish silently.
		logger.warning("database_session_close_cancelled", **_pool_status())
	except Exception:
		logger.exception("Failed to close a database session")


async def get_db() -> AsyncGenerator[AsyncSession, None]:
	"""Get async database session.

	A request leaves its session through three paths, and each one has to hand the
	connection back: a normal return, an exception (including the `HTTPException` behind
	every 401 and 429, which is thrown into this generator), and a cancellation, which
	arrives as a `CancelledError` thrown in as well.
	"""
	session = AsyncSessionLocal()
	try:
		yield session
	except BaseException as exc:
		if session.in_transaction():
			await _rollback_or_ignore_closed_connection(session)
		if isinstance(exc, asyncio.CancelledError):
			# A close awaited here would be interrupted and leak its connection for
			# good; the shielded close finishes it even though this task is going away.
			await _discard_session(session)
		raise
	else:
		await _close_or_report_exhaustion(session, "closed session")
