"""Database session management."""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from .base import engine

AsyncSessionLocal = async_sessionmaker(
	engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db() -> AsyncSession:
	"""Get async database session."""
	async with AsyncSessionLocal() as session:
		yield session