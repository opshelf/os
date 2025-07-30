"""Authentication dependencies for FastAPI routes."""
from typing import Optional, List
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from ..settings import get_settings
from ..database.session import get_db
from ..database.models import User
from ..core.auth import AuthService
from ..core.permissions import has_permission

security = HTTPBearer(auto_error=False)

async def get_current_user_optional(
	credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
	db: Session = Depends(get_db)
) -> Optional[User]:
	"""Get current user if authenticated, otherwise None (for dev mode)."""
	settings = get_settings()
	
	if settings.auth.dev_mode and settings.auth.dev_auto_user:
		user = db.query(User).filter_by(
			email=settings.auth.dev_auto_user,
			is_active=True
		).first()
		if user is not None:
			return user
	
	if credentials is None:
		return None
	
	auth_service = AuthService(
		settings.auth.jwt_secret,
		settings.auth.jwt_algorithm
	)
	
	payload = auth_service.decode_token(credentials.credentials)
	if payload is None or payload.get("type") != "access":
		return None
	
	user = db.query(User).filter_by(
		id=payload.get("user_id"),
		is_active=True
	).first()
	
	return user

async def get_current_user(
	user: Optional[User] = Depends(get_current_user_optional)
) -> User:
	"""Get current authenticated user or raise 401."""
	settings = get_settings()
	
	if not settings.auth.enabled or not settings.auth.require_auth:
		if user is not None:
			return user
		
		if settings.auth.dev_mode and settings.auth.dev_auto_user:
			raise HTTPException(
				status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
				detail="Dev auto-user not found in database"
			)
	
	if user is None:
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Not authenticated",
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