from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.database.models import Role, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_settings


@pytest.fixture
async def viewer_headers(seeded_db) -> dict[str, str]:
	async with AsyncSessionLocal() as db:
		role = (await db.execute(select(Role).where(Role.name == "viewer"))).scalar_one()
		user = (
			await db.execute(select(User).where(User.email == "permission-viewer@opshelf.com"))
		).scalar_one_or_none()
		if user is None:
			settings = get_settings()
			auth = AuthService(settings.auth.jwt_secret or "", settings.auth.jwt_algorithm)
			user = User(
				email="permission-viewer@opshelf.com",
				first_name="Permission",
				last_name="Viewer",
				pin_hash=auth.hash_pin("0000"),
				role_id=role.id,
				is_active=True,
			)
			db.add(user)
			await db.commit()
			await db.refresh(user)

	settings = get_settings()
	auth = AuthService(settings.auth.jwt_secret or "", settings.auth.jwt_algorithm)
	token = auth.create_access_token(
		user_id=user.id,
		email=user.email,
		role=role.name,
		permissions=role.permissions,
		expires_minutes=5,
	)
	return {"Authorization": f"Bearer {token}"}


@pytest.mark.anyio
@pytest.mark.parametrize(
	("method", "path"),
	[
		("GET", "/api/camera"),
		("GET", "/api/iobox/sizer/status"),
		("GET", "/api/calibration/status"),
		("GET", "/api/robot/debug/scanner/status"),
		("GET", "/api/robot/debug/scale"),
		("GET", "/api/robot/debug/motors"),
		("POST", "/api/robot/debug/door?action=home"),
		("GET", "/api/wifi/status"),
		("GET", "/api/telemetry"),
		("GET", "/api/system/home-on-init"),
	],
)
async def test_non_ui_hardware_routes_require_robot_control(
	unauthenticated_client: AsyncClient,
	viewer_headers: dict[str, str],
	method: str,
	path: str,
) -> None:
	response = await unauthenticated_client.request(method, path, headers=viewer_headers)

	assert response.status_code == 403
	assert response.json()["detail"] == "Missing required permission: robot:control"


@pytest.mark.anyio
async def test_system_mode_requires_system_admin(
	unauthenticated_client: AsyncClient,
	viewer_headers: dict[str, str],
) -> None:
	response = await unauthenticated_client.put(
		"/api/system/mode",
		headers=viewer_headers,
		json={"mode": "production"},
	)

	assert response.status_code == 403
	assert response.json()["detail"] == "Missing required permission: system:admin"
