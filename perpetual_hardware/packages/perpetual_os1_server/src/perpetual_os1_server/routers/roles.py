"""Role management API endpoints."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel

from ..database.session import get_db
from ..database.models import Role, User
from .auth_deps import get_current_user, require_permissions

router = APIRouter(
	prefix="/api/roles",
	tags=["roles"],
	dependencies=[Depends(require_permissions("roles:read"))]
)

class RoleCreate(BaseModel):
	"""Role creation request."""
	name: str
	permissions: List[str]

class RoleUpdate(BaseModel):
	"""Role update request."""
	name: Optional[str] = None
	permissions: Optional[List[str]] = None

class RoleResponse(BaseModel):
	"""Role response model."""
	id: int
	name: str
	permissions: List[str]
	user_count: int
	created_at: str
	updated_at: str

async def _build_role_response(db: AsyncSession, role: Role, user_count: int | None = None) -> RoleResponse:
	if user_count is None:
		user_count = int(await db.scalar(select(func.count()).select_from(User).filter(User.role_id == role.id)) or 0)

	return RoleResponse(
		id=role.id,
		name=role.name,
		permissions=role.permissions,
		user_count=user_count,
		created_at=role.created_at.isoformat(),
		updated_at=role.updated_at.isoformat(),
	)

@router.get("", response_model=List[RoleResponse])
async def list_roles(
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> List[RoleResponse]:
	"""List all roles."""
	roles_result = await db.execute(select(Role))
	roles = roles_result.scalars().all()
	
	counts_result = await db.execute(select(User.role_id, func.count()).group_by(User.role_id))
	user_counts = {role_id: int(count) for role_id, count in counts_result.all()}

	return [await _build_role_response(db, role, user_count=user_counts.get(role.id, 0)) for role in roles]

@router.post("", response_model=RoleResponse, dependencies=[Depends(require_permissions("roles:write"))])
async def create_role(
	request: RoleCreate,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> RoleResponse:
	"""Create a new role."""
	existing_result = await db.execute(select(Role).filter(Role.name == request.name))
	existing = existing_result.scalar_one_or_none()
	if existing is not None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Role with this name already exists"
		)
	
	role = Role(
		name=request.name,
		permissions=request.permissions
	)
	
	db.add(role)
	await db.commit()
	await db.refresh(role)
	
	return await _build_role_response(db, role, user_count=0)

@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
	role_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> RoleResponse:
	"""Get a specific role."""
	role_result = await db.execute(select(Role).filter(Role.id == role_id))
	role = role_result.scalar_one_or_none()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Role not found"
		)
	
	return await _build_role_response(db, role)

@router.put("/{role_id}", response_model=RoleResponse, dependencies=[Depends(require_permissions("roles:write"))])
async def update_role(
	role_id: int,
	request: RoleUpdate,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> RoleResponse:
	"""Update a role."""
	role_result = await db.execute(select(Role).filter(Role.id == role_id))
	role = role_result.scalar_one_or_none()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Role not found"
		)
	
	if request.name is not None:
		existing_result = await db.execute(
			select(Role).filter(
				Role.name == request.name,
				Role.id != role_id
			)
		)
		existing = existing_result.scalar_one_or_none()
		if existing is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Role name already in use"
			)
		role.name = request.name
	
	if request.permissions is not None:
		role.permissions = request.permissions
	
	await db.commit()
	await db.refresh(role)
	
	return await _build_role_response(db, role)

@router.delete("/{role_id}", dependencies=[Depends(require_permissions("roles:delete"))])
async def delete_role(
	role_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Delete a role."""
	role_result = await db.execute(select(Role).filter(Role.id == role_id))
	role = role_result.scalar_one_or_none()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Role not found"
		)
	
	user_count = await db.scalar(select(func.count()).select_from(User).filter(User.role_id == role_id))
	if user_count is not None and user_count > 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Cannot delete role with {user_count} active users"
		)
	
	await db.delete(role)
	await db.commit()
	
	return {"message": "Role deleted successfully"}