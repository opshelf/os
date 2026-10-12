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
from ..core.logging_config import get_logger
from ..database.session import AsyncSessionLocal, get_db
from ..database.models import User, Item, Role
from ..core.auth import AuthService
from ..core.permissions import has_permission

# Retain the existing header extraction behavior while documenting JSON login
# accurately: this API does not implement an OAuth password grant.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login/json", auto_error=False)
oauth2_scheme.scheme_name = "UserBearer"
RELAY_SERVICE_TOKEN_SHA256_ENV = "OPSHELF_RELAY_SERVICE_TOKEN_SHA256"

# The dev fallback can fail on every request once the database lacks the
# configured identity, so log the reason once per process instead of per request.
logger = get_logger(__name__)
_did_report_missing_dev_auto_user = False


def get_token_from_request(
	bearer_token: Optional[str] = Depends(oauth2_scheme),
	query_token: Optional[str] = Query(None, alias="token", description="User access token for streaming/image clients. A bearer header takes precedence. Relay service credentials are header-only."),
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
			global _did_report_missing_dev_auto_user
			if _did_report_missing_dev_auto_user is False:
				_did_report_missing_dev_auto_user = True
				logger.warning(
					"dev_auto_user_not_found",
					email=settings.auth.dev_auto_user,
					action="answering 503 for unauthenticated requests",
				)
			raise HTTPException(
				status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
				detail=f"Dev auto-user {settings.auth.dev_auto_user} not found in database"
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


async def get_current_user_for_stream(
	token: Optional[str] = Depends(get_token_from_request),
) -> User:
	"""Authenticate a long-lived response without parking a pooled connection on it.

	`Depends(get_current_user)` hands the endpoint a session that FastAPI closes only
	once the response body is finished, because a dependency with `yield` exits through
	the request exit stack after streaming ends. That is fine for a JSON reply and wrong
	for a stream: an MJPEG view or an SSE session kept open by the dev UI pins an
	aiosqlite connection out of the five-connection pool for the whole view, and the
	bounded uvicorn shutdown drain (`timeout_graceful_shutdown`) abandons that request
	with the connection still checked out. The garbage collector then finalizes it once
	its worker thread is gone, which logs
	"The garbage collector is trying to clean up non-checked-in connection ... which
	will be dropped, as it cannot be safely terminated", and streams can starve the
	pool for every other request.

	Looking the user up in a session owned by this dependency closes it before the body
	starts streaming, so a stream holds no connection at all. The returned user carries
	its `role` and `access_level` already loaded (see the eager loads in
	`get_current_user`) and sessions never commit, so the attributes stay readable after
	close; long-lived routes authenticate on it and never pass it to the database.
	"""
	async with AsyncSessionLocal() as db:
		return await get_current_user(token=token, db=db)


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


async def get_relay_or_current_user_for_stream(
	request: Request,
	token: Optional[str] = Depends(get_token_from_request),
) -> User:
	"""Authenticate a long-lived response the same way `get_relay_or_current_user`
	does, without parking a pooled connection on it (see `get_current_user_for_stream`).

	`/api/robot/status` and `/api/robot/events` are the streams the dev UI keeps open
	for a whole session, so this is the dependency that most often held a connection.
	"""
	async with AsyncSessionLocal() as db:
		return await get_relay_or_current_user(request=request, token=token, db=db)


def ensure_permissions(user: User, permissions: str | List[str]) -> None:
	"""Raise 403 unless the user's role grants every permission."""
	if isinstance(permissions, str):
		permissions = [permissions]

	user_permissions = user.role.permissions or []
	for permission in permissions:
		if not has_permission(user_permissions, permission):
			raise HTTPException(
				status_code=status.HTTP_403_FORBIDDEN,
				detail=f"Missing required permission: {permission}"
			)


def require_permissions(permissions: str | List[str]) -> Callable:
	"""Dependency to require specific permissions."""
	async def permission_checker(
		current_user: User = Depends(get_current_user)
	) -> User:
		"""Check if user has required permissions."""
		ensure_permissions(current_user, permissions)
		return current_user

	return permission_checker


def require_permissions_for_stream(permissions: str | List[str]) -> Callable:
	"""`require_permissions` for routes that may answer with a long-lived stream, so
	the check holds no pooled connection (see `get_current_user_for_stream`)."""
	async def permission_checker(
		current_user: User = Depends(get_current_user_for_stream)
	) -> User:
		ensure_permissions(current_user, permissions)
		return current_user

	return permission_checker


def require_relay_or_permissions(permissions: str | List[str]) -> Callable:
	"""Allow the relay service on explicitly opted-in routes, otherwise enforce user permissions."""
	async def permission_checker(
		request: Request,
		current_user: User = Depends(get_relay_or_current_user),
	) -> User:
		if getattr(request.state, "relay_service_authenticated", False):
			return current_user

		ensure_permissions(current_user, permissions)
		return current_user

	return permission_checker


async def require_relay_or_admin(
	request: Request,
	current_user: User = Depends(get_relay_or_current_user),
) -> User:
	"""Allow the relay service credential or an administrator."""
	if getattr(request.state, "relay_service_authenticated", False):
		return current_user
	if current_user.role.name != "admin":
		raise HTTPException(
			status_code=status.HTTP_403_FORBIDDEN,
			detail="Only the relay or an administrator can do this",
		)
	return current_user


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
