"""Database base configuration and engine setup."""
from sqlalchemy.ext.asyncio import create_async_engine
from typing import Any
from sqlalchemy.orm import declarative_base
from sqlalchemy import event
import os

from openshelf_os.runtime_state import RuntimeStatePaths

Base = declarative_base()

if os.getenv("DATABASE_URL"):
	SQLALCHEMY_DATABASE_URL: str = os.getenv("DATABASE_URL") or ""
else:
	DB_PATH = RuntimeStatePaths.current().database_path
	DB_PATH.parent.mkdir(parents=True, exist_ok=True)
	SQLALCHEMY_DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(
	SQLALCHEMY_DATABASE_URL,
	connect_args={"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {},
	echo=False,
)

@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
	if "sqlite" not in SQLALCHEMY_DATABASE_URL:
		return
	cursor = dbapi_connection.cursor()
	cursor.execute("PRAGMA foreign_keys=ON")
	cursor.close()

if "sqlite" in SQLALCHEMY_DATABASE_URL:
	@event.listens_for(engine.sync_engine, "connect")
	def _set_wal_mode(dbapi_connection: Any, connection_record: Any) -> None:  # pragma: no cover - requires sqlite
		cursor = dbapi_connection.cursor()
		cursor.execute("PRAGMA journal_mode=WAL")
		cursor.close()


def get_database_url() -> str:
	"""Get the database URL for synchronous operations like Alembic migrations."""
	# Swap the driver only. Rewriting the `//` as well added a slash, which pointed a
	# relative path (and `:memory:`) at a different database than the one the app serves.
	if SQLALCHEMY_DATABASE_URL.startswith("sqlite+aiosqlite:"):
		return SQLALCHEMY_DATABASE_URL.replace("sqlite+aiosqlite:", "sqlite:", 1)
	return SQLALCHEMY_DATABASE_URL
