"""Access levels API endpoints."""
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from ..database.session import get_db
from ..database.models import AccessLevel, User
from .auth_deps import get_current_user

router = APIRouter(
	prefix="/api/access-levels",
	tags=["access-levels"]
)

class AccessLevelResponse(BaseModel):
	"""Access level response model."""
	id: int
	name: str
	created_at: str
	updated_at: str

@router.get("", response_model=List[AccessLevelResponse])
async def list_access_levels(
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> List[AccessLevelResponse]:
	"""List all access levels."""
	result = await db.execute(select(AccessLevel).order_by(AccessLevel.id))
	access_levels = result.scalars().all()

	return [
		AccessLevelResponse(
			id=level.id,
			name=str(level.name),
			created_at=level.created_at.isoformat() if level.created_at else "",
			updated_at=level.updated_at.isoformat() if level.updated_at else ""
		)
		for level in access_levels
	]
