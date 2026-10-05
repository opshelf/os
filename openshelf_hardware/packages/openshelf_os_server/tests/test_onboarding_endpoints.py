from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.database.models import AuditLog, Role, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import onboarding
from openshelf_os_server.settings import get_settings


@pytest.fixture(autouse=True)
def completed_onboarding_state() -> Generator[None, None, None]:
    onboarding._write_onboarding_state(onboarding.ONBOARDING_COMPLETED)
    yield
    onboarding._write_onboarding_state(onboarding.ONBOARDING_COMPLETED)


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

    onboarding._write_onboarding_state(onboarding.ONBOARDING_PENDING)

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


async def _add_regular_user(email: str, pin: str = "1111", role_name: str = "operator") -> int:
    async with AsyncSessionLocal() as db:
        role_result = await db.execute(select(Role).filter(Role.name == role_name))
        role = role_result.scalar_one()
        settings = get_settings()
        auth_service = AuthService(
            settings.auth.jwt_secret or "",
            settings.auth.jwt_algorithm,
        )
        user = User(
            email=email,
            first_name="Second",
            last_name="User",
            pin_hash=auth_service.hash_pin(pin),
            role_id=role.id,
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user.id


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
    assert status_resp.json() == {"needs_onboarding": True}

    login_resp = await unauthenticated_client.post(
        "/api/auth/login/json", json={"email": "owner@example.com", "pin": "4321"}
    )
    assert login_resp.status_code == 200, login_resp.text
    access_token = login_resp.json()["access_token"]

    unauthenticated_complete_resp = await unauthenticated_client.post("/api/onboarding/complete")
    assert unauthenticated_complete_resp.status_code == 401

    complete_resp = await unauthenticated_client.post(
        "/api/onboarding/complete",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert complete_resp.json() == {"needs_onboarding": False}

    status_resp = await unauthenticated_client.get("/api/onboarding/status")
    assert status_resp.json() == {"needs_onboarding": False}


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
async def test_pending_admin_user_retry_resets_the_interrupted_admin_pin(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    first_resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": "4321"},
    )
    assert first_resp.status_code == 200, first_resp.text

    # The wizard created this admin and was reloaded before /complete, so its PIN step
    # runs again. The retry must take the account over, not strand the device on 409.
    retry_resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": "9876"},
    )
    assert retry_resp.status_code == 200, retry_resp.text
    assert retry_resp.json()["id"] == first_resp.json()["id"]
    assert retry_resp.json()["role_name"] == "admin"

    status_resp = await unauthenticated_client.get("/api/onboarding/status")
    assert status_resp.json() == {"needs_onboarding": True}

    stale_login = await unauthenticated_client.post(
        "/api/auth/login/json", json={"email": "owner@example.com", "pin": "4321"}
    )
    assert stale_login.status_code == 401

    new_login = await unauthenticated_client.post(
        "/api/auth/login/json", json={"email": "owner@example.com", "pin": "9876"}
    )
    assert new_login.status_code == 200, new_login.text


@pytest.mark.anyio
async def test_pending_admin_user_retry_rejects_a_different_email(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    create_resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": "4321"},
    )
    assert create_resp.status_code == 200, create_resp.text

    # The reset is scoped to the account the pending installation already holds.
    other_resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "intruder@example.com", "pin": "0000"},
    )
    assert other_resp.status_code == 409

    owner_login = await unauthenticated_client.post(
        "/api/auth/login/json", json={"email": "owner@example.com", "pin": "4321"}
    )
    assert owner_login.status_code == 200


@pytest.mark.anyio
async def test_pending_admin_user_retry_rejects_a_second_regular_user(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    create_resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": "4321"},
    )
    assert create_resp.status_code == 200, create_resp.text
    await _add_regular_user("second@example.com", pin="1111")

    # A pending device that already holds another regular user is not a first run:
    # keep 409 rather than let an unauthenticated caller reset the admin PIN.
    retry_resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "owner@example.com", "pin": "9876"},
    )
    assert retry_resp.status_code == 409

    owner_login = await unauthenticated_client.post(
        "/api/auth/login/json", json={"email": "owner@example.com", "pin": "4321"}
    )
    assert owner_login.status_code == 200


@pytest.mark.anyio
async def test_state_failure_returns_fixed_503_without_filesystem_details(
    client: AsyncClient,
    unauthenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_read() -> None:
        raise RuntimeError("Could not read onboarding state from /app/data/.onboarding_state")

    monkeypatch.setattr(onboarding, "_read_onboarding_state", broken_read)

    status_resp = await unauthenticated_client.get("/api/onboarding/status")
    assert status_resp.status_code == 503
    detail = status_resp.json()["detail"]
    assert detail["code"] == "onboarding_state_unavailable"
    assert detail["message"] == "Onboarding state unavailable"
    assert "/app/data" not in detail["message"]

    def broken_write(_value: str) -> None:
        raise OSError(13, "Permission denied", "/app/data/.onboarding_state.tmp")

    monkeypatch.setattr(onboarding, "_write_onboarding_state", broken_write)

    complete_resp = await client.post("/api/onboarding/complete")
    assert complete_resp.status_code == 503
    detail = complete_resp.json()["detail"]
    assert detail["code"] == "onboarding_state_unavailable"
    assert detail["message"] == "Onboarding state unavailable"
    assert "/app/data" not in detail["message"]
