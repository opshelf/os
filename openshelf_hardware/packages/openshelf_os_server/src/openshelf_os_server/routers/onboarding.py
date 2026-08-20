"""First-run onboarding endpoints.

These endpoints are intentionally unauthenticated: they bootstrap a fresh
device that has no users yet. Every endpoint except /status refuses to work
once a regular (non-system) user exists.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..core.auth import AuthService
from ..database.models import User, Role
from ..database.session import get_db
from ..settings import get_settings
from . import wifi
from .users import UserResponse, _build_user_response

router = APIRouter(tags=["onboarding"])

# uvicorn runs a single process (see main.start), so this lock plus the
# re-count inside it is enough to serialize concurrent admin-user requests;
# the unique email constraint is the backstop.
_admin_user_lock = asyncio.Lock()


class OnboardingStatus(BaseModel):
	needs_onboarding: bool


class AdminUserCreate(BaseModel):
	email: EmailStr
	pin: str
	first_name: str = "Admin"
	last_name: str = "User"

	@field_validator('pin')
	@classmethod
	def validate_pin(cls, v: str) -> str:
		if v.isdigit() is False:
			raise ValueError('PIN must contain only digits')
		if len(v) != 4:
			raise ValueError('PIN must be exactly 4 digits')
		return v


async def _count_regular_users(db: AsyncSession) -> int:
	result = await db.execute(select(func.count()).select_from(User).filter(~User.is_system))
	return int(result.scalar() or 0)


async def require_first_run(db: AsyncSession = Depends(get_db)) -> None:
	if await _count_regular_users(db) > 0:
		raise HTTPException(
			status_code=status.HTTP_403_FORBIDDEN,
			detail="Device is already provisioned"
		)


@router.get("/status", response_model=OnboardingStatus)
async def get_onboarding_status(db: AsyncSession = Depends(get_db)) -> OnboardingStatus:
	return OnboardingStatus(needs_onboarding=await _count_regular_users(db) == 0)


@router.get("/wifi/status", response_model=wifi.WifiStatus, dependencies=[Depends(require_first_run)])
async def get_wifi_status() -> wifi.WifiStatus:
	return await wifi.active_status()


@router.get("/wifi/networks", response_model=wifi.WifiNetworkList, dependencies=[Depends(require_first_run)])
async def list_wifi_networks() -> wifi.WifiNetworkList:
	return await wifi.scan_networks()


@router.post("/wifi/connect", response_model=wifi.WifiConnectResponse, dependencies=[Depends(require_first_run)])
async def connect_wifi(request: wifi.WifiConnectRequest) -> wifi.WifiConnectResponse:
	return await wifi.perform_connect(request.ssid, request.password)


@router.post("/admin-user", response_model=UserResponse)
async def create_admin_user(
	request: AdminUserCreate,
	db: AsyncSession = Depends(get_db)
) -> UserResponse:
	async with _admin_user_lock:
		if await _count_regular_users(db) > 0:
			raise HTTPException(
				status_code=status.HTTP_403_FORBIDDEN,
				detail="Device is already provisioned"
			)

		role_result = await db.execute(select(Role).filter(Role.name == "admin"))
		admin_role = role_result.scalar_one_or_none()
		if admin_role is None:
			raise HTTPException(
				status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
				detail="Admin role is not available yet"
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
			role_id=admin_role.id,
			is_active=True
		)

		db.add(user)
		await db.commit()
		await db.refresh(user)

		user_result = await db.execute(
			select(User).options(selectinload(User.role), selectinload(User.access_level)).filter(User.id == user.id)
		)
		return _build_user_response(user_result.scalar_one())
