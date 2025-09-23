"""User management API endpoints."""
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
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
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserListResponse:
	"""List all users with pagination."""
	query = select(User).options(selectinload(User.role))
	
	if is_active is not None:
		query = query.filter(User.is_active == is_active)
	
	if role_id is not None:
		query = query.filter(User.role_id == role_id)
	
	count_query = select(func.count()).select_from(User)
	if is_active is not None:
		count_query = count_query.filter(User.is_active == is_active)
	if role_id is not None:
		count_query = count_query.filter(User.role_id == role_id)
	
	total_result = await db.execute(count_query)
	total = total_result.scalar()
	
	users_result = await db.execute(query.offset(offset).limit(limit))
	users = users_result.scalars().all()
	
	return UserListResponse(
		users=[
			UserResponse(
				id=user.id,
				email=str(user.email),
				first_name=str(user.first_name),
				last_name=str(user.last_name),
				role_id=user.role_id,
				role_name=str(user.role.name) if user.role.name else "",
				is_active=bool(user.is_active),
				created_at=user.created_at.isoformat() if user.created_at else "",
				updated_at=user.updated_at.isoformat() if user.updated_at else ""
			)
			for user in users
		],
		total=int(total or 0),
		offset=offset,
		limit=limit
	)

@router.post("", response_model=UserResponse, dependencies=[Depends(require_permissions("users:write"))])
async def create_user(
	request: UserCreate,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Create a new user."""
	existing_result = await db.execute(select(User).filter(User.email == request.email))
	existing = existing_result.scalar_one_or_none()
	if existing is not None:
		raise HTTPException(
			status_code=status.HTTP_400_BAD_REQUEST,
			detail="User with this email already exists"
		)
	
	role_result = await db.execute(select(Role).filter(Role.id == request.role_id))
	role = role_result.scalar_one_or_none()
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
	await db.commit()
	await db.refresh(user)
	
	# Reload with role relationship
	user_result = await db.execute(select(User).options(selectinload(User.role)).filter(User.id == user.id))
	user = user_result.scalar_one()
	
	return UserResponse(
		id=user.id,
		email=str(user.email),
		first_name=str(user.first_name),
		last_name=str(user.last_name),
		role_id=user.role_id,
		role_name=str(user.role.name) if user.role.name else "",
		is_active=bool(user.is_active),
		created_at=user.created_at.isoformat() if user.created_at else "",
		updated_at=user.updated_at.isoformat() if user.updated_at else ""
	)

@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
	user_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Get a specific user."""
	user_result = await db.execute(select(User).options(selectinload(User.role)).filter(User.id == user_id))
	user = user_result.scalar_one_or_none()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found"
		)
	
	return UserResponse(
		id=user.id,
		email=str(user.email),
		first_name=str(user.first_name),
		last_name=str(user.last_name),
		role_id=user.role_id,
		role_name=str(user.role.name) if user.role.name else "",
		is_active=bool(user.is_active),
		created_at=user.created_at.isoformat() if user.created_at else "",
		updated_at=user.updated_at.isoformat() if user.updated_at else ""
	)

@router.put("/{user_id}", response_model=UserResponse, dependencies=[Depends(require_permissions("users:write"))])
async def update_user(
	user_id: int,
	request: UserUpdate,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Update a user."""
	user_result = await db.execute(select(User).options(selectinload(User.role)).filter(User.id == user_id))
	user = user_result.scalar_one_or_none()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found"
		)
	
	if request.email is not None:
		existing_result = await db.execute(
			select(User).filter(
				User.email == request.email,
				User.id != user_id
			)
		)
		existing = existing_result.scalar_one_or_none()
		if existing is not None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Email already in use"
			)
		user.email = str(request.email)
	
	if request.first_name is not None:
		user.first_name = str(request.first_name)

	if request.last_name is not None:
		user.last_name = str(request.last_name)
	
	if request.pin is not None:
		settings = get_settings()
		auth_service = AuthService(
			settings.auth.jwt_secret,
			settings.auth.jwt_algorithm
		)
		user.pin_hash = str(auth_service.hash_pin(request.pin))
	
	if request.role_id is not None:
		role_result = await db.execute(select(Role).filter(Role.id == request.role_id))
		role = role_result.scalar_one_or_none()
		if role is None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Role not found"
			)
		user.role_id = int(request.role_id)

	if request.is_active is not None:
		user.is_active = bool(request.is_active)
	
	await db.commit()
	await db.refresh(user)
	
	# Reload with role relationship
	user_result = await db.execute(select(User).options(selectinload(User.role)).filter(User.id == user.id))
	user = user_result.scalar_one()
	
	return UserResponse(
		id=user.id,
		email=str(user.email),
		first_name=str(user.first_name),
		last_name=str(user.last_name),
		role_id=user.role_id,
		role_name=str(user.role.name) if user.role.name else "",
		is_active=bool(user.is_active),
		created_at=user.created_at.isoformat() if user.created_at else "",
		updated_at=user.updated_at.isoformat() if user.updated_at else ""
	)

@router.delete("/{user_id}", dependencies=[Depends(require_permissions("users:delete"))])
async def delete_user(
	user_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Soft delete a user by setting inactive."""
	user_result = await db.execute(select(User).options(selectinload(User.role)).filter(User.id == user_id))
	user = user_result.scalar_one_or_none()
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
	await db.commit()
	
	return {"message": "User deactivated successfully"}