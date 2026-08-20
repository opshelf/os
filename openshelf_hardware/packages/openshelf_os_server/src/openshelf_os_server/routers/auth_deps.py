"""Authentication dependencies for FastAPI routes."""
import hashlib
import hmac
import os
from typing import Optional, List, Callable
from fastapi import Depends, HTTPException, status, Query, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..settings import get_settings
from ..database.session import get_db
from ..database.models import User, Item, Role
from ..core.auth import AuthService
from ..core.permissions import has_permission

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
RELAY_SERVICE_TOKEN_SHA256_ENV = "OPSHELF_RELAY_SERVICE_TOKEN_SHA256"


def get_token_from_request(
	bearer_token: Optional[str] = Depends(oauth2_scheme),
	query_token: Optional[str] = Query(None, alias="token"),
) -> Optional[str]:
	"""Extract token from Authorization header or query parameter."""
	if bearer_token is not None:
		return bearer_token
	return query_token

async def get_current_user_optional(
	token: Optional[str] = Depends(get_token_from_request),
	db: AsyncSession = Depends(get_db)
) -> Optional[User]:
	"""Get current user if authenticated, otherwise None (for dev mode)."""
	settings = get_settings()
	
	# First try to get user from token if provided
	if token is not None:
		auth_service = AuthService(
			settings.auth.jwt_secret or "",
			settings.auth.jwt_algorithm
		)
		
		payload = auth_service.decode_token(token)
		if payload is not None and payload.get("type") == "access":
			result = await db.execute(
				select(User)
				.options(selectinload(User.role), selectinload(User.access_level))
				.filter_by(
					id=payload.get("user_id"),
					is_active=True
				)
			)
			user = result.scalar_one_or_none()
			if user is not None:
				return user

	# Fall back to dev auto-user if configured and no valid token
	if settings.auth.dev_mode and settings.auth.dev_auto_user:
		result = await db.execute(
			select(User)
			.options(selectinload(User.role), selectinload(User.access_level))
			.filter_by(
				email=settings.auth.dev_auto_user,
				is_active=True
			)
		)
		user = result.scalar_one_or_none()
		if user is not None:
			return user
	
	return None

async def get_current_user(
	token: Optional[str] = Depends(get_token_from_request),
	db: AsyncSession = Depends(get_db)
) -> User:
	"""Get current authenticated user or raise 401."""
	settings = get_settings()
	
	# Check if auth is disabled
	if not settings.auth.enabled or not settings.auth.require_auth:
		user = await get_current_user_optional(token, db)
		if user is not None:
			return user
		
		if settings.auth.dev_mode and settings.auth.dev_auto_user:
			raise HTTPException(
				status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
				detail="Dev auto-user not found in database"
			)
	
	if not token:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Not authenticated",
			headers={"WWW-Authenticate": "Bearer"},
		)
	
	auth_service = AuthService(
		settings.auth.jwt_secret or "",
		settings.auth.jwt_algorithm
	)

	payload = auth_service.decode_token(token)
	if payload is None or payload.get("type") != "access":
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Invalid token",
			headers={"WWW-Authenticate": "Bearer"},
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
			detail="User not found or inactive",
			headers={"WWW-Authenticate": "Bearer"},
		)

	return user


def _is_relay_service_token(request: Request, token: str) -> bool:
	"""Validate a relay service bearer token without accepting it from query parameters."""
	authorization = request.headers.get("Authorization", "")
	scheme, separator, header_token = authorization.partition(" ")
	if separator == "" or scheme.lower() != "bearer" or header_token != token:
		return False

	expected_digest = os.getenv(RELAY_SERVICE_TOKEN_SHA256_ENV, "")
	if len(expected_digest) != 64 or any(char not in "0123456789abcdef" for char in expected_digest):
		return False

	actual_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
	return hmac.compare_digest(actual_digest, expected_digest)


async def _get_relay_system_user(db: AsyncSession) -> User:
	result = await db.execute(
		select(User)
		.join(Role, User.role_id == Role.id)
		.options(selectinload(User.role), selectinload(User.access_level))
		.where(
			User.is_system.is_(True),
			User.is_active.is_(True),
			Role.name == "admin",
		)
		.order_by(User.id)
	)
	user = result.scalars().first()
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
			detail="Relay service identity is unavailable",
		)
	return user


async def get_relay_or_current_user(
	request: Request,
	token: Optional[str] = Depends(get_token_from_request),
	db: AsyncSession = Depends(get_db),
) -> User:
	"""Authenticate either the scoped relay service credential or a normal user JWT."""
	request.state.relay_service_authenticated = False
	if token is not None and _is_relay_service_token(request, token):
		request.state.relay_service_authenticated = True
		return await _get_relay_system_user(db)
	return await get_current_user(token, db)


def require_permissions(permissions: str | List[str]) -> Callable:
	"""Dependency to require specific permissions."""
	if isinstance(permissions, str):
		permissions = [permissions]

	async def permission_checker(
		current_user: User = Depends(get_current_user)
	) -> User:
		"""Check if user has required permissions."""
		user_permissions = current_user.role.permissions or []

		for permission in permissions:
			if not has_permission(user_permissions, permission):
				raise HTTPException(
					status_code=status.HTTP_403_FORBIDDEN,
					detail=f"Missing required permission: {permission}"
				)

		return current_user

	return permission_checker


def require_relay_or_permissions(permissions: str | List[str]) -> Callable:
	"""Allow the relay service on explicitly opted-in routes, otherwise enforce user permissions."""
	if isinstance(permissions, str):
		permissions = [permissions]

	async def permission_checker(
		request: Request,
		current_user: User = Depends(get_relay_or_current_user),
	) -> User:
		if getattr(request.state, "relay_service_authenticated", False):
			return current_user

		user_permissions = current_user.role.permissions or []
		for permission in permissions:
			if not has_permission(user_permissions, permission):
				raise HTTPException(
					status_code=status.HTTP_403_FORBIDDEN,
					detail=f"Missing required permission: {permission}",
				)
		return current_user

	return permission_checker


def can_user_retrieve_item(user: User, item: Item) -> bool:
	"""
	Check if user can retrieve an item based on access level rules.

	Rules:
	1. Admin role users can retrieve any item
	2. Items with no access level can be retrieved by anyone
	3. Users with no access level can only retrieve items with no access level
	4. Users with an access level can retrieve items with same level or no level
	"""
	if user.role.name == "admin":
		return True

	if item.access_level_id is None:
		return True

	if user.access_level_id is None:
		return False

	if user.access_level_id == item.access_level_id:
		return True

	return False
