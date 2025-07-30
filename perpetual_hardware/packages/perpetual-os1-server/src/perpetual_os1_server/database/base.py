"""Database base configuration and engine setup."""
from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

SQLALCHEMY_DATABASE_URL = os.getenv(
	"DATABASE_URL", 
	"sqlite:///./data/auth.db?check_same_thread=False"
)

engine = create_engine(
	SQLALCHEMY_DATABASE_URL,
	connect_args={"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {},
	pool_pre_ping=True,
	echo=False
)

if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
	with engine.connect() as conn:
		conn.execute(text("PRAGMA journal_mode=WAL"))
		conn.commit()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()