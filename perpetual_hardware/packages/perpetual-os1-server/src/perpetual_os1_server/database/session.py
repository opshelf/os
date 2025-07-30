"""Database session management."""
from typing import Generator
from .base import SessionLocal

def get_db() -> Generator:
	"""Get database session."""
	db = SessionLocal()
	try:
		yield db
	finally:
		db.close()