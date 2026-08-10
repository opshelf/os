"""Database base configuration and engine setup."""
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine.url import URL
from typing import Any, cast
from sqlalchemy.orm import declarative_base
from sqlalchemy import event
import os

from openshelf_os.runtime_state import RuntimeStatePaths

Base = declarative_base()

if os.getenv("DATABASE_URL"):
	SQLALCHEMY_DATABASE_URL: str | URL = os.getenv("DATABASE_URL") or ""
else:
	DB_PATH = RuntimeStatePaths.current().database_path
	DB_PATH.parent.mkdir(parents=True, exist_ok=True)
	SQLALCHEMY_DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(
	SQLALCHEMY_DATABASE_URL,
	connect_args={"check_same_thread": False} if isinstance(SQLALCHEMY_DATABASE_URL, str) and "sqlite" in SQLALCHEMY_DATABASE_URL else {},
	echo=False,
)

@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
	if isinstance(SQLALCHEMY_DATABASE_URL, str) and "sqlite" not in SQLALCHEMY_DATABASE_URL:
		return
	cursor = dbapi_connection.cursor()
	cursor.execute("PRAGMA foreign_keys=ON")
	cursor.close()

if isinstance(SQLALCHEMY_DATABASE_URL, str) and "sqlite" in SQLALCHEMY_DATABASE_URL:
	@event.listens_for(engine.sync_engine, "connect")
	def _set_wal_mode(dbapi_connection: Any, connection_record: Any) -> None:  # pragma: no cover - requires sqlite
		cursor = dbapi_connection.cursor()
		cursor.execute("PRAGMA journal_mode=WAL")
		cursor.close()


def get_database_url() -> str:
	"""Get the database URL for synchronous operations like Alembic migrations."""
	if isinstance(SQLALCHEMY_DATABASE_URL, str):
		url_str: str = cast(str, SQLALCHEMY_DATABASE_URL)
		if url_str.startswith("sqlite+aiosqlite://"):
			return url_str.replace("sqlite+aiosqlite://", "sqlite:///")
		return url_str

	url_str_from_obj: str = str(SQLALCHEMY_DATABASE_URL)
	if url_str_from_obj.startswith("sqlite+aiosqlite://"):
		return url_str_from_obj.replace("sqlite+aiosqlite://", "sqlite:///")

	return url_str_from_obj
