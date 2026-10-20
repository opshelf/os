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
        assert response.json()["detail"]["message"] == "Invalid credentials."
        assert response.headers["Retry-After"] == "1"


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
        assert response.json()["detail"]["message"] == "Invalid credentials."


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
