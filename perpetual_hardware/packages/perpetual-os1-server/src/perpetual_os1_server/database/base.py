"""Database base configuration and engine setup."""
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine.url import URL
from typing import Any
from sqlalchemy.orm import declarative_base
from sqlalchemy import event
from pathlib import Path
import os

Base = declarative_base()

# Database path configuration
if os.getenv("DATABASE_URL"):
	SQLALCHEMY_DATABASE_URL: str | URL = os.getenv("DATABASE_URL") or ""
else:
	DB_PATH = Path("data/base.db").resolve()
	DB_PATH.parent.mkdir(parents=True, exist_ok=True)
	SQLALCHEMY_DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(
	SQLALCHEMY_DATABASE_URL or "sqlite+aiosqlite:///./data/base.db",
	connect_args={"check_same_thread": False} if isinstance(SQLALCHEMY_DATABASE_URL, str) and "sqlite" in SQLALCHEMY_DATABASE_URL else {},
	echo=False,
)

# WAL mode for SQLite (sync engine for event listener)
if isinstance(SQLALCHEMY_DATABASE_URL, str) and "sqlite" in SQLALCHEMY_DATABASE_URL:
	@event.listens_for(engine.sync_engine, "connect")
	def set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
		cursor = dbapi_connection.cursor()
		cursor.execute("PRAGMA journal_mode=WAL")
		cursor.close()
