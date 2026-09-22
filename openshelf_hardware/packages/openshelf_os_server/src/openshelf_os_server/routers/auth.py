"""Authentication API endpoints."""
from openshelf_os_server.documentation import MessageResponse, response_docs
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr, Field

from ..settings import get_settings
from ..database.session import get_db
from ..database.models import User
from ..core.auth import AuthService
from .auth_deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
	"""Login request model."""
	email: EmailStr
	pin: str

class TokenResponse(BaseModel):
	"""Token response model."""
	access_token: str
	refresh_token: str
	token_type: str = "bearer"
	expires_in: int = Field(description="Access-token lifetime in seconds from issuance")

class UserResponse(BaseModel):
	"""User response model."""
	id: int
	email: str
	first_name: str
	last_name: str
	role: str
	permissions: list[str]
	access_level: str | None = None
	is_active: bool

class RefreshRequest(BaseModel):
	"""Refresh token request."""
	refresh_token: str

class PublicUserResponse(BaseModel):
	"""Public user information for lock screen."""
	email: str
	firstName: str
	lastName: str
	role: dict
	accessLevel: dict | None = None

@router.get(
    "/users",
    response_model=List[PublicUserResponse],
    summary="List users for login selection",
    responses=response_docs(),
)
async def get_public_users(
	db: AsyncSession = Depends(get_db)
) -> List[PublicUserResponse]:
	"""List users for login selection.

	Public list of active non-system users for the lock screen. Returns names, email, role
	name, and optional access-level name; no PIN hashes or tokens are returned.
	"""
	result = await db.execute(
		select(User)
		.options(selectinload(User.role), selectinload(User.access_level))
		.filter(User.is_active, ~User.is_system)
		.order_by(User.id)
	)
	users = result.scalars().all()
	
	return [
		PublicUserResponse(
			email=user.email,
			firstName=user.first_name,
			lastName=user.last_name,
			role={"name": user.role.name},
			accessLevel={"name": user.access_level.name} if user.access_level else None
		)
		for user in users
	]

@router.post(
    "/login/json",
    response_model=TokenResponse,
    summary="Log in with email and PIN",
    responses=response_docs(errors=(401,)),
)
async def login_json(
	request: LoginRequest,
	db: AsyncSession = Depends(get_db)
) -> TokenResponse:
	"""Log in with email and PIN.

	Exchanges provisioned user credentials for an access token and refresh token. Send JSON,
	not an OAuth form. Use the access token as `Authorization: Bearer <access_token>`.
	`expires_in` is the configured access-token lifetime in seconds. Invalid credentials
	return `401`.
	"""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret or "",
		settings.auth.jwt_algorithm
	)
	
	result = await db.execute(
		select(User)
		.options(selectinload(User.role), selectinload(User.access_level))
		.filter_by(
			email=request.email,
			is_active=True
		)
	)
	user = result.scalar_one_or_none()
	
	if user is None or not auth_service.verify_pin(request.pin, user.pin_hash):
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid email or PIN"
		)
	
	access_token = auth_service.create_access_token(
		user_id=user.id,
		email=user.email,
		role=user.role.name,
		permissions=user.role.permissions,
		access_level=user.access_level.name if user.access_level else None,
		expires_minutes=settings.auth.jwt_expire_minutes
	)
	
	refresh_token = auth_service.create_refresh_token(
		user_id=user.id,
		email=user.email,
		expires_days=settings.auth.jwt_refresh_expire_days
	)
	
	return TokenResponse(
		access_token=access_token,
		refresh_token=refresh_token,
		expires_in=settings.auth.jwt_expire_minutes * 60
	)

@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh a user access token",
    responses=response_docs(errors=(401,)),
)
async def refresh_token(
	request: RefreshRequest,
	db: AsyncSession = Depends(get_db)
) -> TokenResponse:
	"""Refresh a user access token.

	Exchanges a valid refresh token for a new access token using the current user
	role/access settings. Returns the same refresh token, not a rotated token. Expired
	tokens and inactive or absent users return `401`.
	"""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret or "",
		settings.auth.jwt_algorithm
	)
	
	payload = auth_service.decode_token(request.refresh_token)
	if payload is None or payload.get("type") != "refresh":
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid refresh token"
		)
	
	result = await db.execute(
		select(User)
		.options(selectinload(User.role), selectinload(User.access_level))
		.filter_by(
			id=payload.get("user_id"),
			is_active=True
		)
	)
	user = result.scalar_one_or_none()
	
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="User not found or inactive"
		)
	
	access_token = auth_service.create_access_token(
		user_id=user.id,
		email=user.email,
		role=user.role.name,
		permissions=user.role.permissions,
		access_level=user.access_level.name if user.access_level else None,
		expires_minutes=settings.auth.jwt_expire_minutes
	)
	
	return TokenResponse(
		access_token=access_token,
		refresh_token=request.refresh_token,
		expires_in=settings.auth.jwt_expire_minutes * 60
	)

@router.post(
    "/logout",
    summary="Acknowledge user logout",
    responses=response_docs(model=MessageResponse, errors=(401,)),
)
async def logout(
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Acknowledge user logout.

	Returns a logout acknowledgment for the authenticated user. Tokens are not revoked or
	blacklisted by this endpoint; the client must discard its access and refresh tokens.
	"""
	return {"message": "Logged out successfully"}

@router.get(
    "/me",
    response_model=UserResponse,
    summary="Read the authenticated user",
    responses=response_docs(errors=(401,)),
)
async def get_current_user_info(
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Read the authenticated user.

	Returns the active user, current role permissions, and optional access level. This is
	the account selected by authentication, including the configured development auto-user
	when that fallback is active.
	"""
	return UserResponse(
		id=int(current_user.id),
		email=str(current_user.email),
		first_name=str(current_user.first_name),
		last_name=str(current_user.last_name),
		role=current_user.role.name,
		permissions=current_user.role.permissions,
		access_level=current_user.access_level.name if current_user.access_level else None,
		is_active=bool(current_user.is_active)
	)
