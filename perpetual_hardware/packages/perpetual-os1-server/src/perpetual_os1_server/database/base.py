"""Database base configuration and engine setup."""
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy import event
from pathlib import Path
import os

Base = declarative_base()

# Database path configuration
if os.getenv("DATABASE_URL"):
	SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL")
else:
	DB_PATH = Path("data/auth.db").resolve()
	DB_PATH.parent.mkdir(parents=True, exist_ok=True)
	SQLALCHEMY_DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"

engine = create_async_engine(
	SQLALCHEMY_DATABASE_URL,
	connect_args={"check_same_thread": False} if "sqlite" in SQLALCHEMY_DATABASE_URL else {},
	echo=False,
)

# WAL mode for SQLite (sync engine for event listener)
if "sqlite" in SQLALCHEMY_DATABASE_URL:
	@event.listens_for(engine.sync_engine, "connect")
	def set_sqlite_pragma(dbapi_connection, connection_record):
		cursor = dbapi_connection.cursor()
		cursor.execute("PRAGMA journal_mode=WAL")
		cursor.close()