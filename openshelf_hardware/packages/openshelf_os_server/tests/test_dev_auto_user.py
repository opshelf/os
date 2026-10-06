"""The dev-mode fallback identity must name a user that every build creates.

A miss is not fatal at startup, but every unauthenticated request then answers
503 "Dev auto-user ... not found in database", which is what the dev UI shows as
DEV AUTO-USER NOT FOUND IN DATABASE in the startup homing row.
"""
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from openshelf_os_server import main
from openshelf_os_server.database.models import AuditLog, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import auth_deps
from openshelf_os_server.settings import DEVELOPER_SYSTEM_USER_EMAIL


class _StubAuthSettings:
    def __init__(self, dev_auto_user: str) -> None:
        self.enabled = True
        self.require_auth = False
        self.dev_mode = True
        self.dev_auto_user = dev_auto_user


class _StubSettings:
    def __init__(self, dev_auto_user: str) -> None:
        self.auth = _StubAuthSettings(dev_auto_user)


def _use_dev_settings(monkeypatch: pytest.MonkeyPatch, dev_auto_user: str) -> None:
    monkeypatch.setattr(auth_deps, "get_settings", lambda: _StubSettings(dev_auto_user))


@pytest_asyncio.fixture
async def developer_system_user(seeded_db: AsyncGenerator[None, None]) -> AsyncGenerator[None, None]:
    """Seed the developer system user exactly as startup does, then remove it.

    seeded_db is session-scoped, so the row must not outlive this test. Requests
    made with that identity leave audit rows behind, and audit_logs.user_id has no
    ondelete rule, so the audit rows go first.
    """
    await main._ensure_developer_user()
    yield
    async with AsyncSessionLocal() as db:
        user_id = (
            await db.execute(select(User.id).where(User.email == DEVELOPER_SYSTEM_USER_EMAIL))
        ).scalar_one_or_none()
        if user_id is not None:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


@pytest.mark.anyio
async def test_seeded_developer_user_resolves(developer_system_user: AsyncGenerator[None, None]) -> None:
    assert await main._dev_auto_user_is_resolvable(DEVELOPER_SYSTEM_USER_EMAIL) is True


@pytest.mark.anyio
async def test_missing_dev_auto_user_answers_503_naming_the_email(
    seeded_db: AsyncGenerator[None, None],
    unauthenticated_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_dev_settings(monkeypatch, "nobody@opshelf.com")

    response = await unauthenticated_client.get("/api/system/home-on-init")

    assert response.status_code == 503
    assert response.json()["detail"]["message"] == "Dev auto-user nobody@opshelf.com not found in database"


