"""Authentication dependencies for FastAPI routes."""
from typing import Optional, List
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from ..settings import get_settings
from ..database.session import get_db
from ..database.models import User
from ..core.auth import AuthService
from ..core.permissions import has_permission

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

async def get_current_user_optional(
	token: Optional[str] = Depends(oauth2_scheme),
	db: AsyncSession = Depends(get_db)
) -> Optional[User]:
	"""Get current user if authenticated, otherwise None (for dev mode)."""
	settings = get_settings()
	
	# First try to get user from token if provided
	if token is not None:
		auth_service = AuthService(
			settings.auth.jwt_secret,
			settings.auth.jwt_algorithm
		)
		
		payload = auth_service.decode_token(token)
		if payload is not None and payload.get("type") == "access":
			result = await db.execute(
				select(User)
				.options(selectinload(User.role))
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
			.options(selectinload(User.role))
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
	token: Optional[str] = Depends(oauth2_scheme),
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
		settings.auth.jwt_secret,
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
		.options(selectinload(User.role))
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

def require_permissions(permissions: str | List[str]):
	"""Dependency to require specific permissions."""
	if isinstance(permissions, str):
		permissions = [permissions]
	
	async def permission_checker(
		current_user: User = Depends(get_current_user)
	) -> User:
		"""Check if user has required permissions."""
		user_permissions = current_user.role.permissions
		
		for permission in permissions:
			if not has_permission(user_permissions, permission):
				raise HTTPException(
					status_code=status.HTTP_403_FORBIDDEN,
					detail=f"Missing required permission: {permission}"
				)
		
		return current_user
	
	return permission_checker