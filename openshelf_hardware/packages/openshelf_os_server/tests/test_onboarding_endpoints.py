from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from openshelf_os_server.main import _ensure_developer_user
from openshelf_os_server.database.models import AuditLog, Role, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import wifi


@pytest_asyncio.fixture
async def fresh_device(seeded_db: AsyncGenerator[None, None]) -> AsyncGenerator[None, None]:
    """Simulate a fresh device by hiding existing users behind the is_system flag.

    Onboarding only counts non-system users, so flipping the flag avoids
    deleting rows that audit logs and other tables reference by id.
    """
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        snapshot = {user.id: bool(user.is_system) for user in result.scalars().all()}
        await db.execute(update(User).values(is_system=True))
        await db.commit()

    yield

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User))
        created_ids = [user.id for user in result.scalars().all() if user.id not in snapshot]
        if len(created_ids) > 0:
            await db.execute(
                update(AuditLog).where(AuditLog.user_id.in_(created_ids)).values(user_id=None)
            )
            await db.execute(delete(User).where(User.id.in_(created_ids)))
        for user_id, is_system in snapshot.items():
            await db.execute(update(User).where(User.id == user_id).values(is_system=is_system))
        await db.commit()


async def _add_system_user() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Role).filter(Role.name == "admin"))
        admin_role = result.scalar_one()
        user_result = await db.execute(
            select(User).filter(User.email == "developer@opshelf.com")
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            db.add(
                User(
                    email="developer@opshelf.com",
                    first_name="Developer",
                    last_name="System",
                    pin_hash="not-a-real-hash",
                    role_id=admin_role.id,
                    is_active=True,
                    is_system=True,
                )
            )
        else:
            user.is_system = True
        await db.commit()


@pytest.mark.anyio
async def test_status_needs_onboarding_on_fresh_device(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    resp = await unauthenticated_client.get("/api/onboarding/status")
    assert resp.status_code == 200
    assert resp.json() == {"needs_onboarding": True}


@pytest.mark.anyio
async def test_status_ignores_system_users(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    await _add_system_user()
    resp = await unauthenticated_client.get("/api/onboarding/status")
    assert resp.status_code == 200
    assert resp.json() == {"needs_onboarding": True}


@pytest.mark.anyio
async def test_status_provisioned_when_user_exists(unauthenticated_client: AsyncClient) -> None:
    resp = await unauthenticated_client.get("/api/onboarding/status")
    assert resp.status_code == 200
    assert resp.json() == {"needs_onboarding": False}


@pytest.mark.anyio
async def test_create_admin_user_and_login(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": "4321"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["email"] == "owner@example.com"
    assert data["role_name"] == "admin"
    assert data["is_active"] is True

    status_resp = await unauthenticated_client.get("/api/onboarding/status")
    assert status_resp.json() == {"needs_onboarding": False}

    login_resp = await unauthenticated_client.post(
        "/api/auth/login/json", json={"email": "owner@example.com", "pin": "4321"}
    )
    assert login_resp.status_code == 200, login_resp.text
    assert "access_token" in login_resp.json()


@pytest.mark.anyio
async def test_create_admin_user_rejected_when_provisioned(
    unauthenticated_client: AsyncClient,
) -> None:
    resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "second@example.com", "pin": "4321"},
    )
    assert resp.status_code == 403


@pytest.mark.anyio
@pytest.mark.parametrize("pin", ["12a4", "123", "12345", ""])
async def test_create_admin_user_rejects_invalid_pin(
    unauthenticated_client: AsyncClient, fresh_device: None, pin: str
) -> None:
    resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": pin},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_wifi_endpoints_rejected_when_provisioned(
    unauthenticated_client: AsyncClient,
) -> None:
    networks_resp = await unauthenticated_client.get("/api/onboarding/wifi/networks")
    assert networks_resp.status_code == 403

    status_resp = await unauthenticated_client.get("/api/onboarding/wifi/status")
    assert status_resp.status_code == 403

    connect_resp = await unauthenticated_client.post(
        "/api/onboarding/wifi/connect", json={"ssid": "Test", "password": "secret"}
    )
    assert connect_resp.status_code == 403


@pytest.mark.anyio
async def test_ensure_developer_user_recreates_after_wipe(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    await _ensure_developer_user()
    await _ensure_developer_user()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).filter(User.email == "developer@opshelf.com"))
        developers = result.scalars().all()
        assert len(developers) == 1
        assert bool(developers[0].is_system) is True

    resp = await unauthenticated_client.get("/api/onboarding/status")
    assert resp.json() == {"needs_onboarding": True}


@pytest.mark.anyio
async def test_wifi_passthrough_on_fresh_device(
    unauthenticated_client: AsyncClient,
    fresh_device: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_scan() -> wifi.WifiNetworkList:
        return wifi.WifiNetworkList(
            networks=[wifi.WifiNetwork(ssid="Lab", signal=90, security="WPA2", in_use=False)]
        )

    async def fake_connect(ssid: str, password: str) -> wifi.WifiConnectResponse:
        return wifi.WifiConnectResponse(success=True, ssid=ssid, message=f"connected to {ssid}")

    async def fake_status() -> wifi.WifiStatus:
        return wifi.WifiStatus(connected=False, ssid=None, signal=None, ip=None)

    monkeypatch.setattr(wifi, "scan_networks", fake_scan)
    monkeypatch.setattr(wifi, "perform_connect", fake_connect)
    monkeypatch.setattr(wifi, "active_status", fake_status)

    networks_resp = await unauthenticated_client.get("/api/onboarding/wifi/networks")
    assert networks_resp.status_code == 200
    assert networks_resp.json() == {
        "networks": [{"ssid": "Lab", "signal": 90, "security": "WPA2", "in_use": False}]
    }

    status_resp = await unauthenticated_client.get("/api/onboarding/wifi/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["connected"] is False

    connect_resp = await unauthenticated_client.post(
        "/api/onboarding/wifi/connect", json={"ssid": "Lab", "password": "hunter22"}
    )
    assert connect_resp.status_code == 200
    assert connect_resp.json() == {"success": True, "ssid": "Lab", "message": "connected to Lab"}
