from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, func, select

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.core import login as login_core
from openshelf_os_server.core.login import login_backoff
from openshelf_os_server.database.models import AuditLog, RobotEvent, Role, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import users as users_router
from openshelf_os_server.settings import get_settings


MakeUser = Callable[..., Awaitable[User]]
AddRobotEvent = Callable[..., Awaitable[RobotEvent]]


@pytest_asyncio.fixture
async def make_user() -> AsyncGenerator[MakeUser, None]:
    created_ids: list[int] = []

    async def create(
        *,
        email: str | None = None,
        first_name: str = "Login",
        last_name: str = "Tester",
        pin: str = "4321",
        is_active: bool = True,
        is_system: bool = False,
        login_barcode: str | None = None,
    ) -> User:
        settings = get_settings()
        auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
        async with AsyncSessionLocal() as db:
            role = await db.scalar(select(Role).where(Role.name == "operator"))
            assert role is not None
            user = User(
                email=email or f"operator-{uuid4().hex}@example.com",
                first_name=first_name,
                last_name=last_name,
                pin_hash=auth.hash_pin(pin),
                role_id=role.id,
                is_active=is_active,
                is_system=is_system,
                login_barcode=login_barcode,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            created_ids.append(user.id)
            return user

    login_backoff.reset()
    yield create
    login_backoff.reset()
    async with AsyncSessionLocal() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id.in_(created_ids)))
        await db.execute(delete(User).where(User.id.in_(created_ids)))
        await db.commit()


@pytest_asyncio.fixture
async def add_robot_event() -> AsyncGenerator[AddRobotEvent, None]:
    created_seqs: list[int] = []

    async def create(
        payload: dict,
        *,
        created_at: datetime | None = None,
    ) -> RobotEvent:
        async with AsyncSessionLocal() as db:
            seq = int(await db.scalar(select(func.coalesce(func.max(RobotEvent.seq), 0))) or 0) + 1
            event = RobotEvent(seq=seq, payload=payload)
            if created_at is not None:
                event.created_at = created_at
            db.add(event)
            await db.commit()
            await db.refresh(event)
            created_seqs.append(seq)
            return event

    yield create
    async with AsyncSessionLocal() as db:
        await db.execute(delete(RobotEvent).where(RobotEvent.seq.in_(created_seqs)))
        await db.commit()


@pytest.mark.anyio
async def test_login_experience_switches_at_four_active_non_system_users(
    unauthenticated_client: AsyncClient,
    make_user: MakeUser,
) -> None:
    await make_user(is_active=False)
    await make_user(is_system=True)
    for _ in range(2):
        await make_user()

    experience = await unauthenticated_client.get("/api/auth/experience")
    assert experience.status_code == 200
    assert experience.json() == {"mode": "cards", "active_user_count": 3}

    public_users = await unauthenticated_client.get("/api/auth/users")
    assert public_users.status_code == 200
    assert len(public_users.json()) == 3
    assert all(
        set(user) == {"email", "firstName", "lastName", "role", "accessLevel"}
        for user in public_users.json()
    )

    await make_user()
    experience = await unauthenticated_client.get("/api/auth/experience")
    assert experience.json() == {"mode": "identifier", "active_user_count": 4}
    assert (await unauthenticated_client.get("/api/auth/users")).status_code == 404


@pytest.mark.anyio
async def test_identifier_login_accepts_email_and_unique_full_name(
    unauthenticated_client: AsyncClient,
    make_user: MakeUser,
) -> None:
    user = await make_user(
        email="CaseSensitive@example.com",
        first_name="Ada",
        last_name="Lovelace",
    )

    by_email = await unauthenticated_client.post(
        "/api/auth/login/json",
        json={"identifier": "casesensitive@EXAMPLE.com", "pin": "4321"},
    )
    assert by_email.status_code == 200

    by_name = await unauthenticated_client.post(
        "/api/auth/login/json",
        json={"identifier": "ADA LOVELACE", "pin": "4321"},
    )
    assert by_name.status_code == 200

    async with AsyncSessionLocal() as db:
        log = await db.scalar(
            select(AuditLog)
            .where(AuditLog.endpoint == "/api/auth/login/json")
            .order_by(AuditLog.id.desc())
        )
    assert log is not None
    assert log.user_id == user.id
    assert log.request_body == {"identifier": "[REDACTED]", "pin": "[REDACTED]"}


@pytest.mark.anyio
async def test_ambiguous_name_and_unknown_identity_fail_generically(
    unauthenticated_client: AsyncClient,
    make_user: MakeUser,
) -> None:
    await make_user(first_name="Duplicate", last_name="Name")
    await make_user(first_name="Duplicate", last_name="Name")

    for identifier in ("Duplicate Name", "nobody@example.com"):
        response = await unauthenticated_client.post(
            "/api/auth/login/json",
            json={"identifier": identifier, "pin": "4321"},
        )
        assert response.status_code == 401
        assert response.json()["detail"]["message"] == "Invalid credentials"
        assert response.headers["Retry-After"] == "1"


@pytest.mark.anyio
async def test_scan_token_login_resolves_the_event_barcode(
    unauthenticated_client: AsyncClient,
    make_user: MakeUser,
    add_robot_event: AddRobotEvent,
) -> None:
    barcode = f"USR-{uuid4().hex}"
    await make_user(login_barcode=barcode)
    event = await add_robot_event({"kind": "label_scanned", "payload": barcode})
    settings = get_settings()
    auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
    token = auth.create_scan_token(event.seq, login_core.SCAN_TOKEN_TTL_S)
    claims = auth.decode_token(token)
    assert claims is not None
    assert claims["seq"] == event.seq
    assert "barcode" not in claims
    assert barcode not in token

    response = await unauthenticated_client.post(
        "/api/auth/login/json",
        json={"scan_token": token, "pin": "4321"},
    )
    assert response.status_code == 200
    assert barcode not in response.text


@pytest.mark.anyio
async def test_scan_token_failures_are_generic(
    unauthenticated_client: AsyncClient,
    make_user: MakeUser,
    add_robot_event: AddRobotEvent,
) -> None:
    barcode = f"USR-{uuid4().hex}"
    await make_user(login_barcode=barcode)
    stale = await add_robot_event(
        {"kind": "label_scanned", "payload": barcode},
        created_at=datetime.now(timezone.utc)
        - timedelta(seconds=login_core.SCAN_EVENT_MAX_AGE_S + 1),
    )
    not_a_scan = await add_robot_event({"kind": "command_completed", "payload": barcode})
    settings = get_settings()
    auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
    tokens = (
        auth.create_scan_token(stale.seq, login_core.SCAN_TOKEN_TTL_S),
        auth.create_scan_token(not_a_scan.seq, login_core.SCAN_TOKEN_TTL_S),
        auth.create_scan_token(2_000_000_000, login_core.SCAN_TOKEN_TTL_S),
        auth.create_scan_token(stale.seq, -1),
    )

    for token in tokens:
        response = await unauthenticated_client.post(
            "/api/auth/login/json",
            json={"scan_token": token, "pin": "4321"},
        )
        assert response.status_code == 401
        assert response.json()["detail"]["message"] == "Invalid credentials"


@pytest.mark.anyio
async def test_scan_frames_filter_events_and_never_expose_the_barcode() -> None:
    settings = get_settings()
    auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
    barcode = f"USR-{uuid4().hex}"
    ignored = RobotEvent(seq=40, payload={"kind": "command_completed", "payload": barcode})
    scan = RobotEvent(seq=41, payload={"kind": "label_scanned", "payload": barcode})
    reads: list[tuple[int, int]] = []

    async def read(since_seq: int, limit: int) -> list[RobotEvent]:
        reads.append((since_seq, limit))
        return [ignored, scan]

    frames = login_core.scan_token_frames(auth, 39, poll_s=0, read=read)
    frame = await anext(frames)
    await frames.aclose()

    assert reads == [(39, login_core.SCAN_STREAM_PAGE_LIMIT)]
    assert set(frame) == {"scan_token", "expires_in"}
    assert frame["expires_in"] == login_core.SCAN_TOKEN_TTL_S
    assert auth.decode_scan_token(frame["scan_token"]) == scan.seq
    assert barcode not in json.dumps(frame)


@pytest.mark.anyio
async def test_login_barcode_print_always_rotates_assignment(
    client: AsyncClient,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user()
    endpoint = f"/api/users/{user.id}/login-barcode/print"
    monkeypatch.setattr(users_router, "print_payload", lambda _payload: True)

    assigned = await client.post(endpoint)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json() == {
        "user_id": user.id,
        "printed": True,
        "assigned": True,
        "replaced": False,
        "has_login_barcode": True,
    }
    async with AsyncSessionLocal() as db:
        first_barcode = (await db.get(User, user.id)).login_barcode
    assert first_barcode is not None
    assert first_barcode not in assigned.text

    replaced = await client.post(endpoint)
    assert replaced.status_code == 200
    assert replaced.json()["assigned"] is False
    assert replaced.json()["replaced"] is True
    async with AsyncSessionLocal() as db:
        second_barcode = (await db.get(User, user.id)).login_barcode
    assert second_barcode is not None
    assert second_barcode != first_barcode


@pytest.mark.anyio
async def test_login_barcode_resolves_managed_user_without_exposing_barcode(
    client: AsyncClient,
    make_user: MakeUser,
) -> None:
    barcode = f"USR-{uuid4().hex}"
    user = await make_user(login_barcode=barcode, is_active=False)

    resolved = await client.post(
        "/api/users/login-barcode/resolve",
        json={"login_barcode": barcode},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["id"] == user.id
    assert resolved.json()["has_login_barcode"] is True
    assert barcode not in resolved.text

    missing = await client.post(
        "/api/users/login-barcode/resolve",
        json={"login_barcode": "USR-not-assigned"},
    )
    assert missing.status_code == 404


@pytest.mark.anyio
async def test_failed_print_keeps_assigned_barcode_for_safe_retry(
    client: AsyncClient,
    make_user: MakeUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = await make_user()
    endpoint = f"/api/users/{user.id}/login-barcode/print"
    monkeypatch.setattr(users_router, "print_payload", lambda _payload: False)

    failed = await client.post(endpoint)
    assert failed.status_code == 503
    assert failed.json()["detail"]["login_barcode_assigned"] is True
    async with AsyncSessionLocal() as db:
        assigned_barcode = (await db.get(User, user.id)).login_barcode
    assert assigned_barcode is not None

    monkeypatch.setattr(users_router, "print_payload", lambda _payload: True)
    retried = await client.post(endpoint)
    assert retried.status_code == 200
    assert retried.json()["assigned"] is False
    async with AsyncSessionLocal() as db:
        assert (await db.get(User, user.id)).login_barcode != assigned_barcode
