"""Access levels API endpoints."""
from openshelf_os_server.documentation import response_docs
from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel

from ..database.session import get_db
from ..database.models import AccessLevel, User
from .auth_deps import get_current_user

router = APIRouter(
	prefix="/api/access_levels",
	tags=["access_levels"]
)

class AccessLevelResponse(BaseModel):
	"""Access level response model."""
	id: int
	name: str
	created_at: str
	updated_at: str

@router.get(
    "",
    response_model=List[AccessLevelResponse],
    summary="List access levels",
    responses=response_docs(errors=(401,)),
)
async def list_access_levels(
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> List[AccessLevelResponse]:
	"""List access levels.

	Returns the configured access levels used by users and inventory retrieval checks.
	Requires authentication. Assign access-level IDs through the user and item endpoints.
	"""
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
