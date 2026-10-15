"""User management API endpoints."""
import asyncio
from openshelf_os_server.documentation import MessageResponse, response_docs
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr, Field, field_validator

from ..database.session import get_db
from ..database.models import User, Role, AccessLevel
from ..core.auth import AuthService
from ..core.login import generate_login_barcode
from ..settings import get_settings
from .auth_deps import get_current_user, require_permissions
from .printer import _print_payload as print_payload

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
	access_level_id: Optional[int] = None

	@field_validator('pin')
	@classmethod
	def validate_pin(cls, v: str) -> str:
		if v.isdigit() is False:
			raise ValueError('PIN must contain only digits')
		if len(v) != 4:
			raise ValueError('PIN must be exactly 4 digits')
		return v

class UserUpdate(BaseModel):
	"""User update request."""
	email: Optional[EmailStr] = None
	first_name: Optional[str] = None
	last_name: Optional[str] = None
	pin: Optional[str] = None
	role_id: Optional[int] = None
	access_level_id: Optional[int] = None
	is_active: Optional[bool] = None

	@field_validator('pin')
	@classmethod
	def validate_pin(cls, v: Optional[str]) -> Optional[str]:
		if v is not None:
			if v.isdigit() is False:
				raise ValueError('PIN must contain only digits')
			if len(v) != 4:
				raise ValueError('PIN must be exactly 4 digits')
		return v

class UserResponse(BaseModel):
	"""User response model."""
	id: int
	email: str
	first_name: str
	last_name: str
	role_id: int
	role_name: str
	access_level_id: Optional[int] = None
	access_level_name: Optional[str] = None
	is_active: bool
	has_login_barcode: bool
	created_at: str
	updated_at: str


class LoginBarcodeResolveRequest(BaseModel):
	login_barcode: str = Field(min_length=1, max_length=64)


class LoginBarcodePrintResponse(BaseModel):
	user_id: int
	printed: bool
	assigned: bool
	replaced: bool
	has_login_barcode: bool

class UserListResponse(BaseModel):
	"""User list response."""
	users: List[UserResponse]
	total: int
	offset: int
	limit: int

def _build_user_response(user: User) -> UserResponse:
	return UserResponse(
		id=user.id,
		email=str(user.email),
		first_name=str(user.first_name),
		last_name=str(user.last_name),
		role_id=user.role_id,
		role_name=str(user.role.name) if user.role.name else "",
		access_level_id=user.access_level_id,
		access_level_name=str(user.access_level.name) if user.access_level and user.access_level.name else None,
		is_active=bool(user.is_active),
		has_login_barcode=user.login_barcode is not None,
		created_at=user.created_at.isoformat() if user.created_at else "",
		updated_at=user.updated_at.isoformat() if user.updated_at else "",
	)

@router.get(
    "",
    response_model=UserListResponse,
    summary="List managed users",
    responses=response_docs(errors=(401, 403)),
)
async def list_users(
	offset: int = Query(0, ge=0),
	limit: int = Query(100, ge=1, le=1000),
	is_active: Optional[bool] = None,
	role_id: Optional[int] = None,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserListResponse:
	"""List managed users.

	Returns an offset page of non-system users with optional active-state and role filters.
	Requires `users:read`; the total reflects the same filters.
	"""
	query = select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(~User.is_system)

	if is_active is not None:
		query = query.filter(User.is_active == is_active)

	if role_id is not None:
		query = query.filter(User.role_id == role_id)

	count_query = select(func.count()).select_from(User).filter(~User.is_system)
	if is_active is not None:
		count_query = count_query.filter(User.is_active == is_active)
	if role_id is not None:
		count_query = count_query.filter(User.role_id == role_id)
	
	total_result = await db.execute(count_query)
	total = total_result.scalar()
	
	users_result = await db.execute(query.offset(offset).limit(limit))
	users = users_result.scalars().all()
	
	return UserListResponse(
		users=[_build_user_response(user) for user in users],
		total=int(total or 0),
		offset=offset,
		limit=limit
	)

@router.post(
    "",
    response_model=UserResponse,
    dependencies=[Depends(require_permissions("users:write"))],
    summary="Create a user",
    responses=response_docs(errors=(400, 401, 403)),
)
async def create_user(
	request: UserCreate,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Create a user.

	Requires both `users:read` and `users:write`. PINs contain exactly four digits and are
	stored hashed. Email must be unique; role and optional access level must exist. Invalid
	references or duplicate email return `400`.
	"""
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

	if request.access_level_id is not None:
		access_level_result = await db.execute(select(AccessLevel).filter(AccessLevel.id == request.access_level_id))
		access_level = access_level_result.scalar_one_or_none()
		if access_level is None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Access level not found"
			)
	
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret or "",
		settings.auth.jwt_algorithm
	)

	user = User(
		email=request.email,
		first_name=request.first_name,
		last_name=request.last_name,
		pin_hash=auth_service.hash_pin(request.pin),
		role_id=request.role_id,
		access_level_id=request.access_level_id,  # type: ignore[arg-type]
		is_active=True
	)
	
	db.add(user)
	await db.commit()
	await db.refresh(user)
	
	# Reload with role and access_level relationships
	user_result = await db.execute(select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(User.id == user.id))
	user = user_result.scalar_one()
	
	return _build_user_response(user)

@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Read a managed user",
    responses=response_docs(errors=(401, 403, 404)),
)
async def get_user(
	user_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Read a managed user.

	Requires `users:read`. Returns role/access-level details for a non-system user. Absent
	and system users return `404`.
	"""
	user_result = await db.execute(select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(User.id == user_id, ~User.is_system))
	user = user_result.scalar_one_or_none()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found"
		)
	
	return _build_user_response(user)

@router.patch(
	"/{user_id}",
	response_model=UserResponse,
	dependencies=[Depends(require_permissions("users:write"))],
	summary="Update a managed user",
	responses=response_docs(errors=(400, 401, 403, 404)),
)
@router.put(
    "/{user_id}",
    response_model=UserResponse,
    dependencies=[Depends(require_permissions("users:write"))],
    summary="Update a managed user",
    responses=response_docs(errors=(400, 401, 403, 404)),
)
async def update_user(
	user_id: int,
	request: UserUpdate,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Update a managed user.

	Requires both `users:read` and `users:write`. Supplied non-null values update the
	account; PIN replacement is hashed. Null or omitted values are not a general
	field-clearing mechanism. System users are excluded.
	"""
	user_result = await db.execute(select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(User.id == user_id, ~User.is_system))
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
			settings.auth.jwt_secret or "",
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

	if request.access_level_id is not None:
		access_level_result = await db.execute(select(AccessLevel).filter(AccessLevel.id == request.access_level_id))
		access_level = access_level_result.scalar_one_or_none()
		if access_level is None:
			raise HTTPException(
				status_code=status.HTTP_400_BAD_REQUEST,
				detail="Access level not found"
			)
		user.access_level_id = int(request.access_level_id)

	if request.is_active is not None:
		user.is_active = bool(request.is_active)
	
	await db.commit()
	await db.refresh(user)
	
	# Reload with role and access_level relationships
	user_result = await db.execute(select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(User.id == user.id))
	user = user_result.scalar_one()
	
	return _build_user_response(user)


@router.post(
	"/login-barcode/resolve",
	response_model=UserResponse,
	summary="Resolve a scanned login barcode to a managed user",
	responses=response_docs(errors=(401, 403, 404)),
)
async def resolve_login_barcode(
	request: LoginBarcodeResolveRequest,
	db: AsyncSession = Depends(get_db),
) -> UserResponse:
	"""Resolve an exact assigned barcode without exposing barcode values in responses."""
	user = await db.scalar(
		select(User)
		.options(selectinload(User.role), selectinload(User.access_level))
		.where(User.login_barcode == request.login_barcode, ~User.is_system)
	)
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found",
		)
	return _build_user_response(user)


@router.post(
	"/{user_id}/login-barcode/print",
	response_model=LoginBarcodePrintResponse,
	dependencies=[
		Depends(require_permissions("users:write")),
		Depends(require_permissions("printer:print")),
	],
	summary="Rotate and print a user's login barcode",
	responses=response_docs(errors=(401, 403, 404, 503)),
)
async def print_login_barcode(
	user_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user),
) -> LoginBarcodePrintResponse:
	"""Replace any prior assignment, persist it, then print the new barcode."""
	user = await db.scalar(select(User).where(User.id == user_id, ~User.is_system))
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_404_NOT_FOUND,
			detail="User not found",
		)

	had_barcode = user.login_barcode is not None
	assigned = not had_barcode
	replaced = had_barcode
	user.login_barcode = generate_login_barcode()
	await db.commit()
	await db.refresh(user)

	assert user.login_barcode is not None
	try:
		printed = await asyncio.to_thread(print_payload, user.login_barcode)
	except Exception as exc:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail={
				"message": "Unable to print login barcode",
				"login_barcode_assigned": True,
			},
		) from exc

	if not printed:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail={
				"message": "Unable to print login barcode",
				"login_barcode_assigned": True,
			},
		)

	return LoginBarcodePrintResponse(
		user_id=user.id,
		printed=True,
		assigned=assigned,
		replaced=replaced,
		has_login_barcode=True,
	)

@router.delete(
    "/{user_id}",
    dependencies=[Depends(require_permissions("users:delete"))],
    summary="Deactivate a user",
    responses=response_docs(model=MessageResponse, errors=(400, 401, 403, 404)),
)
async def delete_user(
	user_id: int,
	db: AsyncSession = Depends(get_db),
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Deactivate a user.

	Requires both `users:read` and `users:delete`. Soft-deletes a non-system account by
	setting `is_active=false`; it does not erase the user or audit history. Deactivating
	your own account returns `400`.
	"""
	user_result = await db.execute(select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(User.id == user_id, ~User.is_system))
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
