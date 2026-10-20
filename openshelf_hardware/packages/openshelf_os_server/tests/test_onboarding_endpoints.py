from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select, update

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.database.models import AuditLog, Role, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.errors import CODE_MESSAGES
from openshelf_os_server.routers import onboarding, wifi
from openshelf_os_server.routers.wifi import _NmcliResult
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


def _fake_nmcli(monkeypatch: pytest.MonkeyPatch, previous_connection: str | None, restore_fails: bool = False) -> None:
	"""Replace the nmcli subprocess with a scripted failure of the connect."""

	async def fake(*args: str) -> _NmcliResult:
		if args == ("-t", "-f", "NAME,DEVICE", "connection", "show", "--active"):
			return _NmcliResult(0, f"{previous_connection}:wlan0\n" if previous_connection else "", "")
		if args == ("-t", "-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi"):
			return _NmcliResult(0, "", "")
		if args == ("-w", "30", "connection", "up", wifi.PENDING_NAME):
			return _NmcliResult(1, "", "authentication failed")
		if previous_connection is not None and args == ("-w", "30", "connection", "up", previous_connection):
			return _NmcliResult(1, "", "restore failed") if restore_fails else _NmcliResult(0, "", "")
		return _NmcliResult(0, "", "")

	monkeypatch.setattr(wifi, "_run_nmcli", fake)


@pytest.mark.anyio
async def test_wifi_connect_failure_is_neutral_code_when_previous_connection_restored(
	client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# A failed connect that rolls back to the previous connection keeps the
	# neutral sentence: the cabinet is still reachable.
	_fake_nmcli(monkeypatch, "OldConn")
	response = await client.post("/api/wifi/connect", json={"ssid": "TestNet", "password": "secret"})
	assert response.status_code == 502
	detail = response.json()["detail"]
	assert detail["code"] == "wifi_connect_failed"
	assert detail["message"] == "The cabinet could not connect to that network. Try again."
	assert "TestNet" not in response.text and "authentication failed" not in response.text


@pytest.mark.anyio
async def test_wifi_connect_failure_is_offline_code_when_nothing_to_restore(
	client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# With no previous connection, the failed connect leaves the cabinet
	# unreachable; the body must say so instead of the neutral sentence.
	_fake_nmcli(monkeypatch, None)
	response = await client.post("/api/wifi/connect", json={"ssid": "TestNet", "password": "secret"})
	assert response.status_code == 502
	detail = response.json()["detail"]
	assert detail["code"] == "wifi_connect_failed_offline"
	assert detail["message"] == CODE_MESSAGES["wifi_connect_failed_offline"]
	assert "may now be offline" in detail["message"]
	assert "TestNet" not in response.text and "authentication failed" not in response.text


@pytest.mark.anyio
async def test_wifi_connect_failure_is_offline_code_when_restore_fails(
	client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	# A failed connect whose rollback to the previous connection also fails
	# leaves the cabinet unreachable, like the nothing-to-restore case.
	_fake_nmcli(monkeypatch, "OldConn", restore_fails=True)
	response = await client.post("/api/wifi/connect", json={"ssid": "TestNet", "password": "secret"})
	assert response.status_code == 502
	detail = response.json()["detail"]
	assert detail["code"] == "wifi_connect_failed_offline"
	assert detail["message"] == CODE_MESSAGES["wifi_connect_failed_offline"]
	assert "may now be offline" in detail["message"]
	assert "TestNet" not in response.text and "restore failed" not in response.text


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
async def test_pending_admin_user_email_conflict_returns_typed_409(
    unauthenticated_client: AsyncClient, fresh_device: None
) -> None:
    # fresh_device hides the seeded admin as a system user, so the regular-user
    # check passes and the INSERT hits the unique email constraint. That account
    # must not be taken over, and the conflict must not leak the raw sqlite error.
    resp = await unauthenticated_client.post(
        "/api/onboarding/admin-user",
        json={"email": "admin@opshelf.com", "pin": "9876"},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {
        "message": "Administrator account is already created.",
        "code": "conflict",
    }

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).filter(User.email == "admin@opshelf.com"))
        admin = result.scalar_one()
    settings = get_settings()
    auth_service = AuthService(settings.auth.jwt_secret or "", settings.auth.jwt_algorithm)
    assert not auth_service.verify_pin("9876", admin.pin_hash)
