"""Authentication API endpoints."""
import json
from typing import AsyncGenerator, Literal

from openshelf_os_server.documentation import MessageResponse, response_docs, stream_docs
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from openshelf_os_server.sse import EventSourceResponse

from ..core.auth import AuthService
from ..core.event_log import latest_robot_event_seq
from ..core.login import (
	credential_backoff_key,
	login_backoff,
	resolve_identifier,
	resolve_login_barcode,
	resolve_scan_token,
	scan_token_frames,
	verify_pin_or_dummy,
)
from ..database.models import User
from ..database.session import get_db
from ..settings import get_settings
from .auth_deps import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

class LoginRequest(BaseModel):
	"""Login request model."""
	email: EmailStr | None = None
	identifier: str | None = Field(default=None, min_length=1, max_length=255)
	scan_token: str | None = Field(default=None, min_length=1, max_length=4096)
	pin: str

	@model_validator(mode="after")
	def validate_identity(self) -> "LoginRequest":
		provided = sum(
			value is not None
			for value in (self.email, self.identifier, self.scan_token)
		)
		if provided != 1:
			raise ValueError("Provide exactly one of email, identifier, or scan_token")
		return self

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


class LoginExperienceResponse(BaseModel):
	"""Lock-screen experience selected from the active user count."""
	mode: Literal["cards", "identifier"]
	active_user_count: int


class PublicUserResponse(BaseModel):
	"""Public user information for the small-account card experience."""
	email: str
	firstName: str
	lastName: str
	role: dict[str, str]
	accessLevel: dict[str, str] | None = None


async def _active_public_user_count(db: AsyncSession) -> int:
	count = await db.scalar(
		select(func.count(User.id)).where(User.is_active, ~User.is_system)
	)
	return int(count or 0)


@router.get(
    "/experience",
    response_model=LoginExperienceResponse,
    summary="Read the automatic lock-screen experience",
    responses=response_docs(),
)
async def get_login_experience(
	db: AsyncSession = Depends(get_db),
) -> LoginExperienceResponse:
	"""Use cards for up to three active users and identifier entry for four or more."""
	active_user_count = await _active_public_user_count(db)
	return LoginExperienceResponse(
		mode="cards" if active_user_count <= 3 else "identifier",
		active_user_count=active_user_count,
	)

@router.get(
    "/users",
    response_model=list[PublicUserResponse],
    summary="List users for the small-account card login experience",
    responses=response_docs(errors=(404,)),
)
async def get_public_users(
	db: AsyncSession = Depends(get_db),
) -> list[PublicUserResponse]:
	"""Return active non-system users only while the card experience applies."""
	result = await db.execute(
		select(User)
		.options(selectinload(User.role), selectinload(User.access_level))
		.where(User.is_active, ~User.is_system)
		.order_by(User.id)
	)
	users = list(result.scalars().all())
	if len(users) > 3:
		raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

	return [
		PublicUserResponse(
			email=user.email,
			firstName=user.first_name,
			lastName=user.last_name,
			role={"name": user.role.name},
			accessLevel={"name": user.access_level.name} if user.access_level else None,
		)
		for user in users
	]


@router.get(
	"/scans",
	summary="Stream signed login scan tokens",
	responses=response_docs(
		existing=stream_docs(
			"text/event-stream",
			"Short-lived signed login scan tokens encoded as JSON in SSE data frames.",
			None,
		),
	),
	response_class=Response,
)
async def stream_login_scans() -> EventSourceResponse:
	"""Stream short-lived signed tokens for new scanner events without exposing barcodes."""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret or "", settings.auth.jwt_algorithm
	)
	start_seq = await latest_robot_event_seq()

	async def event_generator() -> AsyncGenerator[dict[str, str], None]:
		async for frame in scan_token_frames(auth_service, start_seq):
			yield {"data": json.dumps(frame)}

	return EventSourceResponse(
		event_generator(),
		ping=15,
		send_timeout=30,
		headers={"Cache-Control": "no-cache"},
	)


LOGIN_FAILED_DETAIL = "Invalid credentials"

@router.post(
    "/login/json",
    response_model=TokenResponse,
    summary="Log in with an identity and PIN",
    responses=response_docs(errors=(401, 429)),
)
async def login_json(
	payload: LoginRequest,
	request: Request,
	db: AsyncSession = Depends(get_db),
) -> TokenResponse:
	"""Exchange email, name, assigned barcode, or scan token plus PIN for tokens."""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret or "",
		settings.auth.jwt_algorithm
	)

	user: User | None
	if payload.scan_token is not None:
		barcode = await resolve_scan_token(db, auth_service, payload.scan_token)
		backoff_key = credential_backoff_key(
			barcode if barcode is not None else payload.scan_token
		)
		user = (
			await resolve_login_barcode(db, barcode)
			if barcode is not None
			else None
		)
	else:
		identifier = str(payload.email) if payload.email is not None else payload.identifier or ""
		backoff_key = credential_backoff_key(identifier)
		user = await resolve_identifier(db, identifier)

	retry_after = login_backoff.retry_after(backoff_key)
	if retry_after > 0:
		raise HTTPException(
			status_code=status.HTTP_429_TOO_MANY_REQUESTS,
			detail=LOGIN_FAILED_DETAIL,
			headers={"Retry-After": str(retry_after)},
		)

	if not verify_pin_or_dummy(auth_service, payload.pin, user) or user is None:
		retry_after = login_backoff.record_failure(backoff_key)
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail=LOGIN_FAILED_DETAIL,
			headers={"Retry-After": str(retry_after)},
		)

	login_backoff.record_success(backoff_key)
	request.state.authenticated_user_id = user.id

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
