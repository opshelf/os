"""Role management API endpoints."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
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

@router.get("", response_model=List[RoleResponse])
async def list_roles(
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> List[RoleResponse]:
	"""List all roles."""
	roles = db.query(Role).all()
	
	return [
		RoleResponse(
			id=role.id,
			name=role.name,
			permissions=role.permissions,
			user_count=db.query(User).filter_by(role_id=role.id).count(),
			created_at=role.created_at.isoformat(),
			updated_at=role.updated_at.isoformat()
		)
		for role in roles
	]

@router.post("", response_model=RoleResponse, dependencies=[Depends(require_permissions("roles:write"))])
async def create_role(
	request: RoleCreate,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> RoleResponse:
	"""Create a new role."""
	existing = db.query(Role).filter_by(name=request.name).first()
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
	db.commit()
	db.refresh(role)
	
	return RoleResponse(
		id=role.id,
		name=role.name,
		permissions=role.permissions,
		user_count=0,
		created_at=role.created_at.isoformat(),
		updated_at=role.updated_at.isoformat()
	)

@router.get("/{role_id}", response_model=RoleResponse)
async def get_role(
	role_id: int,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> RoleResponse:
	"""Get a specific role."""
	role = db.query(Role).filter_by(id=role_id).first()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Role not found"
		)
	
	return RoleResponse(
		id=role.id,
		name=role.name,
		permissions=role.permissions,
		user_count=db.query(User).filter_by(role_id=role.id).count(),
		created_at=role.created_at.isoformat(),
		updated_at=role.updated_at.isoformat()
	)

@router.put("/{role_id}", response_model=RoleResponse, dependencies=[Depends(require_permissions("roles:write"))])
async def update_role(
	role_id: int,
	request: RoleUpdate,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> RoleResponse:
	"""Update a role."""
	role = db.query(Role).filter_by(id=role_id).first()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Role not found"
		)
	
	if request.name is not None:
		existing = db.query(Role).filter(
			Role.name == request.name,
			Role.id != role_id
		).first()
		if existing is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Role name already in use"
			)
		role.name = request.name
	
	if request.permissions is not None:
		role.permissions = request.permissions
	
	db.commit()
	db.refresh(role)
	
	return RoleResponse(
		id=role.id,
		name=role.name,
		permissions=role.permissions,
		user_count=db.query(User).filter_by(role_id=role.id).count(),
		created_at=role.created_at.isoformat(),
		updated_at=role.updated_at.isoformat()
	)

@router.delete("/{role_id}", dependencies=[Depends(require_permissions("roles:delete"))])
async def delete_role(
	role_id: int,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Delete a role."""
	role = db.query(Role).filter_by(id=role_id).first()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="Role not found"
		)
	
	user_count = db.query(User).filter_by(role_id=role_id).count()
	if user_count > 0:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail=f"Cannot delete role with {user_count} active users"
		)
	
	db.delete(role)
	db.commit()
	
	return {"message": "Role deleted successfully"}