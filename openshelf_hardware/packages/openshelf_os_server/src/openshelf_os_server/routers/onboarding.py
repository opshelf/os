"""First-run onboarding endpoints.

The backend-owned state file is the source of truth for whether onboarding is
pending. Wi-Fi bootstrap routes are intentionally unauthenticated while that
state is pending. Completing onboarding requires the newly created admin to
authenticate.
"""
from openshelf_os_server.documentation import response_docs
from openshelf_os_server.errors import api_error
import asyncio
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openshelf_os.runtime_state import RuntimeStatePaths

from ..core.auth import AuthService
from ..database.models import User, Role
from ..database.session import get_db
from ..settings import get_settings
from . import wifi
from .auth_deps import require_permissions
from .users import UserResponse, _build_user_response

router = APIRouter(tags=["onboarding"])

# uvicorn runs a single process (see main.start), so this lock plus the
# re-count inside it is enough to serialize concurrent admin-user requests;
# the unique email constraint is the backstop.
_admin_user_lock = asyncio.Lock()
_onboarding_state_lock = asyncio.Lock()

ONBOARDING_STATE_PATH_ENV = "OPENSHELF_ONBOARDING_STATE_PATH"
ONBOARDING_PENDING = "pending"
ONBOARDING_COMPLETED = "completed"


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


def _onboarding_state_path() -> Path:
	override = os.getenv(ONBOARDING_STATE_PATH_ENV)
	if override:
		return Path(override).resolve()
	return RuntimeStatePaths.current().data_dir / ".onboarding_state"


def _read_onboarding_state() -> str | None:
	path = _onboarding_state_path()
	try:
		value = path.read_text(encoding="utf-8").strip().lower()
	except FileNotFoundError:
		return None
	except OSError as exc:
		raise RuntimeError(f"Could not read onboarding state from {path}") from exc

	if value not in {ONBOARDING_PENDING, ONBOARDING_COMPLETED}:
		raise RuntimeError(f"Invalid onboarding state in {path}: {value!r}")
	return value


def _write_onboarding_state(value: str) -> None:
	if value not in {ONBOARDING_PENDING, ONBOARDING_COMPLETED}:
		raise ValueError(f"Unsupported onboarding state: {value}")

	path = _onboarding_state_path()
	path.parent.mkdir(parents=True, exist_ok=True)
	temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
	try:
		temp_path.write_text(f"{value}\n", encoding="utf-8")
		os.replace(temp_path, path)
	finally:
		temp_path.unlink(missing_ok=True)


async def ensure_onboarding_state(db: AsyncSession) -> str:
	"""Create explicit state once, migrating already-provisioned installations."""
	try:
		state = _read_onboarding_state()
		if state is not None:
			return state

		async with _onboarding_state_lock:
			state = _read_onboarding_state()
			if state is not None:
				return state

			state = ONBOARDING_COMPLETED if await _count_regular_users(db) > 0 else ONBOARDING_PENDING
			_write_onboarding_state(state)
			return state
	except (RuntimeError, ValueError) as exc:
		# A corrupt or unwritable state file is a device fault, not a client error.
		raise api_error(status.HTTP_503_SERVICE_UNAVAILABLE, "onboarding_state_unavailable", str(exc)) from exc


async def require_first_run(db: AsyncSession = Depends(get_db)) -> None:
	if await ensure_onboarding_state(db) == ONBOARDING_COMPLETED:
		raise HTTPException(
			status_code=status.HTTP_403_FORBIDDEN,
			detail="Onboarding is already completed"
		)


@router.get(
    "/status",
    response_model=OnboardingStatus,
    summary="Read first-run setup state",
    responses=response_docs(),
)
async def get_onboarding_status(db: AsyncSession = Depends(get_db)) -> OnboardingStatus:
	"""Read first-run setup state.

	Public endpoint indicating whether onboarding is pending. The check ensures onboarding
	state exists; it is not a robot health check.
	"""
	return OnboardingStatus(needs_onboarding=await ensure_onboarding_state(db) == ONBOARDING_PENDING)


@router.get(
    "/wifi/status",
    response_model=wifi.WifiStatus,
    dependencies=[Depends(require_first_run)],
    summary="Read Wi-Fi during first-run setup",
    responses=response_docs(errors=(403,)),
)
async def get_wifi_status() -> wifi.WifiStatus:
	"""Read Wi-Fi during first-run setup.

	Public only while onboarding is pending. Returns the active wireless connection state;
	after setup, use the authenticated Wi-Fi endpoint.
	"""
	return await wifi.active_status()


@router.get(
    "/wifi/networks",
    response_model=wifi.WifiNetworkList,
    dependencies=[Depends(require_first_run)],
    summary="Scan Wi-Fi during first-run setup",
    responses=response_docs(errors=(403,)),
)
async def list_wifi_networks() -> wifi.WifiNetworkList:
	"""Scan Wi-Fi during first-run setup.

	Public only while onboarding is pending. Scans for wireless networks; after setup, use
	the authenticated Wi-Fi endpoint.
	"""
	return await wifi.scan_networks()


@router.post(
    "/wifi/connect",
    response_model=wifi.WifiConnectResponse,
    dependencies=[Depends(require_first_run)],
    summary="Connect Wi-Fi during first-run setup",
    responses=response_docs(errors=(403,)),
)
async def connect_wifi(request: wifi.WifiConnectRequest) -> wifi.WifiConnectResponse:
	"""Connect Wi-Fi during first-run setup.

	Public only while onboarding is pending. Activates the supplied connection and persists
	it on success. Changing the device network can disconnect this client; failures roll
	back the pending connection.
	"""
	return await wifi.perform_connect(request.ssid, request.password)


@router.post(
    "/admin-user",
    response_model=UserResponse,
    summary="Create the first administrator",
    responses=response_docs(errors=(403, 409, 503)),
)
async def create_admin_user(
	request: AdminUserCreate,
	db: AsyncSession = Depends(get_db)
) -> UserResponse:
	"""Create the first administrator.

	Public only while onboarding is pending and no non-system administrator exists. Creates
	the initial administrator using the provisioned admin role. Returns `409` when already
	created and `403` after onboarding is complete.
	"""
	async with _admin_user_lock:
		if await ensure_onboarding_state(db) == ONBOARDING_COMPLETED:
			raise HTTPException(
				status_code=status.HTTP_403_FORBIDDEN,
				detail="Onboarding is already completed"
			)

		if await _count_regular_users(db) > 0:
			raise HTTPException(
				status_code=status.HTTP_409_CONFLICT,
				detail="Administrator account is already created"
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


@router.post(
    "/complete",
    response_model=OnboardingStatus,
    dependencies=[Depends(require_permissions("users:write"))],
    summary="Complete first-run setup",
    responses=response_docs(errors=(401, 403)),
)
async def complete_onboarding() -> OnboardingStatus:
	"""Complete first-run setup.

	Requires `users:write`. Marks onboarding complete, disabling the public first-run
	mutation endpoints. Returns `needs_onboarding=false`.
	"""
	_write_onboarding_state(ONBOARDING_COMPLETED)
	return OnboardingStatus(needs_onboarding=False)
