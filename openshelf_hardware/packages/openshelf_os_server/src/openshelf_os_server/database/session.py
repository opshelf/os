"""Database session management."""
import sqlite3
from typing import AsyncGenerator

from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .base import engine

AsyncSessionLocal = async_sessionmaker(
	engine, class_=AsyncSession, expire_on_commit=False
)


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


async def get_db() -> AsyncGenerator[AsyncSession, None]:
	"""Get async database session."""
	session = AsyncSessionLocal()
	try:
		yield session
	except Exception:
		if session.in_transaction():
			await _rollback_or_ignore_closed_connection(session)
		raise
	finally:
		await _close_or_ignore_closed_connection(session)
