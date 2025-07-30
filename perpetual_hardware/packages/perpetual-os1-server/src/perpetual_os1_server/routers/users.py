"""User management API endpoints."""
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr

from ..database.session import get_db
from ..database.models import User, Role
from ..core.auth import AuthService
from ..settings import get_settings
from .auth_deps import get_current_user, require_permissions

router = APIRouter(
	prefix="/api/users",
	tags=["users"],
	dependencies=[Depends(require_permissions("users:read"))]
)

class UserCreate(BaseModel):
	"""User creation request."""
	email: EmailStr
	first_name: str
	last_name: str
	pin: str
	role_id: int

class UserUpdate(BaseModel):
	"""User update request."""
	email: Optional[EmailStr] = None
	first_name: Optional[str] = None
	last_name: Optional[str] = None
	pin: Optional[str] = None
	role_id: Optional[int] = None
	is_active: Optional[bool] = None

class UserResponse(BaseModel):
	"""User response model."""
	id: int
	email: str
	first_name: str
	last_name: str
	role_id: int
	role_name: str
	is_active: bool
	created_at: str
	updated_at: str

class UserListResponse(BaseModel):
	"""User list response."""
	users: List[UserResponse]
	total: int
	offset: int
	limit: int

@router.get("", response_model=UserListResponse)
async def list_users(
	offset: int = Query(0, ge=0),
	limit: int = Query(100, ge=1, le=1000),
	is_active: Optional[bool] = None,
	role_id: Optional[int] = None,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserListResponse:
	"""List all users with pagination."""
	query = db.query(User)
	
	if is_active is not None:
		query = query.filter_by(is_active=is_active)
	
	if role_id is not None:
		query = query.filter_by(role_id=role_id)
	
	total = query.count()
	users = query.offset(offset).limit(limit).all()
	
	return UserListResponse(
		users=[
			UserResponse(
				id=user.id,
				email=user.email,
				first_name=user.first_name,
				last_name=user.last_name,
				role_id=user.role_id,
				role_name=user.role.name,
				is_active=user.is_active,
				created_at=user.created_at.isoformat(),
				updated_at=user.updated_at.isoformat()
			)
			for user in users
		],
		total=total,
		offset=offset,
		limit=limit
	)

@router.post("", response_model=UserResponse, dependencies=[Depends(require_permissions("users:write"))])
async def create_user(
	request: UserCreate,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Create a new user."""
	existing = db.query(User).filter_by(email=request.email).first()
	if existing is not None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="User with this email already exists"
		)
	
	role = db.query(Role).filter_by(id=request.role_id).first()
	if role is None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Role not found"
		)
	
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret,
		settings.auth.jwt_algorithm
	)
	
	user = User(
		email=request.email,
		first_name=request.first_name,
		last_name=request.last_name,
		pin_hash=auth_service.hash_pin(request.pin),
		role_id=request.role_id,
		is_active=True
	)
	
	db.add(user)
	db.commit()
	db.refresh(user)
	
	return UserResponse(
		id=user.id,
		email=user.email,
		first_name=user.first_name,
		last_name=user.last_name,
		role_id=user.role_id,
		role_name=user.role.name,
		is_active=user.is_active,
		created_at=user.created_at.isoformat(),
		updated_at=user.updated_at.isoformat()
	)

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
	user_id: int,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Get a specific user."""
	user = db.query(User).filter_by(id=user_id).first()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found"
		)
	
	return UserResponse(
		id=user.id,
		email=user.email,
		first_name=user.first_name,
		last_name=user.last_name,
		role_id=user.role_id,
		role_name=user.role.name,
		is_active=user.is_active,
		created_at=user.created_at.isoformat(),
		updated_at=user.updated_at.isoformat()
	)

@router.put("/{user_id}", response_model=UserResponse, dependencies=[Depends(require_permissions("users:write"))])
async def update_user(
	user_id: int,
	request: UserUpdate,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Update a user."""
	user = db.query(User).filter_by(id=user_id).first()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found"
		)
	
	if request.email is not None:
		existing = db.query(User).filter(
			User.email == request.email,
			User.id != user_id
		).first()
		if existing is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Email already in use"
			)
		user.email = request.email
	
	if request.first_name is not None:
		user.first_name = request.first_name
	
	if request.last_name is not None:
		user.last_name = request.last_name
	
	if request.pin is not None:
		settings = get_settings()
		auth_service = AuthService(
			settings.auth.jwt_secret,
			settings.auth.jwt_algorithm
		)
		user.pin_hash = auth_service.hash_pin(request.pin)
	
	if request.role_id is not None:
		role = db.query(Role).filter_by(id=request.role_id).first()
		if role is None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Role not found"
			)
		user.role_id = request.role_id
	
	if request.is_active is not None:
		user.is_active = request.is_active
	
	db.commit()
	db.refresh(user)
	
	return UserResponse(
		id=user.id,
		email=user.email,
		first_name=user.first_name,
		last_name=user.last_name,
		role_id=user.role_id,
		role_name=user.role.name,
		is_active=user.is_active,
		created_at=user.created_at.isoformat(),
		updated_at=user.updated_at.isoformat()
	)

@router.delete("/{user_id}", dependencies=[Depends(require_permissions("users:delete"))])
async def delete_user(
	user_id: int,
	db: Session = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Soft delete a user by setting inactive."""
	user = db.query(User).filter_by(id=user_id).first()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found"
		)
	
	if user.id == current_user.id:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="Cannot delete your own account"
		)
	
	user.is_active = False
	db.commit()
	
	return {"message": "User deactivated successfully"}