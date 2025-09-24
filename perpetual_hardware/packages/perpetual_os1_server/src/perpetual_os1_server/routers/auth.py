"""Authentication API endpoints."""
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, status, Form
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel, EmailStr

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
	expires_in: int

class UserResponse(BaseModel):
	"""User response model."""
	id: int
	email: str
	first_name: str
	last_name: str
	role: str
	permissions: list[str]
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

@router.get("/users", response_model=List[PublicUserResponse])
async def get_public_users(
	db: AsyncSession = Depends(get_db)
) -> List[PublicUserResponse]:
	"""
	Get list of active users for lock screen selection.
	Returns limited information (no sensitive data).
	"""
	result = await db.execute(
		select(User)
		.options(selectinload(User.role))
		.filter(User.is_active == True)
		.order_by(User.id)
	)
	users = result.scalars().all()
	
	return [
		PublicUserResponse(
			email=user.email,
			firstName=user.first_name,
			lastName=user.last_name,
			role={"name": user.role.name}
		)
		for user in users
	]

@router.post("/login")
async def login_form(
	form_data: OAuth2PasswordRequestForm = Depends(),
	db: AsyncSession = Depends(get_db)
) -> dict:
	"""OAuth2 compatible login endpoint for form data."""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret,
		settings.auth.jwt_algorithm
	)
	
	result = await db.execute(
		select(User)
		.options(selectinload(User.role))
		.filter_by(
			email=form_data.username,
			is_active=True
		)
	)
	user = result.scalar_one_or_none()
	
	if user is None or not auth_service.verify_pin(form_data.password, user.pin_hash):
		raise HTTPException(
			status_code=status.HTTP_401_UNAUTHORIZED,
			detail="Incorrect email or PIN",
			headers={"WWW-Authenticate": "Bearer"},
		)
	
	access_token = auth_service.create_access_token(
		user_id=user.id,
		email=user.email,
		role=user.role.name,
		permissions=user.role.permissions,
		expires_minutes=settings.auth.jwt_expire_minutes
	)
	
	refresh_token = auth_service.create_refresh_token(
		user_id=user.id,
		email=user.email,
		expires_days=settings.auth.jwt_refresh_expire_days
	)
	
	return {
		"access_token": access_token,
		"refresh_token": refresh_token,
		"token_type": "bearer"
	}

@router.post("/login/json", response_model=TokenResponse)
async def login_json(
	request: LoginRequest,
	db: AsyncSession = Depends(get_db)
) -> TokenResponse:
	"""JSON login endpoint (alternative to form)."""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret,
		settings.auth.jwt_algorithm
	)
	
	result = await db.execute(
		select(User)
		.options(selectinload(User.role))
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

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
	request: RefreshRequest,
	db: AsyncSession = Depends(get_db)
) -> TokenResponse:
	"""Refresh access token using refresh token."""
	settings = get_settings()
	auth_service = AuthService(
		settings.auth.jwt_secret,
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
			detail="User not found or inactive"
		)
	
	access_token = auth_service.create_access_token(
		user_id=user.id,
		email=user.email,
		role=user.role.name,
		permissions=user.role.permissions,
		expires_minutes=settings.auth.jwt_expire_minutes
	)
	
	return TokenResponse(
		access_token=access_token,
		refresh_token=request.refresh_token,
		expires_in=settings.auth.jwt_expire_minutes * 60
	)

@router.post("/logout")
async def logout(
	current_user: User = Depends(get_current_user)
) -> dict:
	"""Logout current user (placeholder for token blacklisting)."""
	return {"message": "Logged out successfully"}

@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
	current_user: User = Depends(get_current_user)
) -> UserResponse:
	"""Get current user information."""
	return UserResponse(
		id=int(current_user.id),
		email=str(current_user.email),
		first_name=str(current_user.first_name),
		last_name=str(current_user.last_name),
		role=current_user.role.name,
		permissions=current_user.role.permissions,
		is_active=bool(current_user.is_active)
	)