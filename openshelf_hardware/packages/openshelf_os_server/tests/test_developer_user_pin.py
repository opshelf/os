"""The developer system user's PIN follows the operating mode.

The repository is public, so production and demo devices must never accept the
well-known 1111: they take DEVELOPER_PIN or lock the user with a random PIN.
"""
from typing import AsyncGenerator

import pytest
from argon2 import PasswordHasher
from sqlalchemy import delete, select

from openshelf_os.common import OperatingMode
from openshelf_os_server import main
from openshelf_os_server.database.models import AuditLog, User
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import DEFAULT_DEVELOPER_PIN, DEVELOPER_PIN_ENV, DEVELOPER_SYSTEM_USER_EMAIL


@pytest.fixture
async def clean_developer_user(seeded_db: AsyncGenerator[None, None]) -> AsyncGenerator[None, None]:
    await _delete_developer_user()
    yield
    await _delete_developer_user()


async def _delete_developer_user() -> None:
    async with AsyncSessionLocal() as db:
        user_id = (
            await db.execute(select(User.id).where(User.email == DEVELOPER_SYSTEM_USER_EMAIL))
        ).scalar_one_or_none()
        if user_id is not None:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def _developer_pin_hash() -> str:
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == DEVELOPER_SYSTEM_USER_EMAIL))
        ).scalar_one()
        return user.pin_hash


def _accepts(pin_hash: str, pin: str) -> bool:
    return main._pin_matches(PasswordHasher(), pin_hash, pin)


def _use_mode(monkeypatch: pytest.MonkeyPatch, mode: OperatingMode) -> None:
    monkeypatch.setattr(main, "get_operating_mode", lambda: mode)


@pytest.mark.anyio
@pytest.mark.parametrize("mode", [OperatingMode.production, OperatingMode.demo])
async def test_production_creates_the_user_without_the_default_pin(
    clean_developer_user: None, monkeypatch: pytest.MonkeyPatch, mode: OperatingMode
) -> None:
    monkeypatch.delenv(DEVELOPER_PIN_ENV, raising=False)
    _use_mode(monkeypatch, mode)

    await main._ensure_developer_user()

    assert _accepts(await _developer_pin_hash(), DEFAULT_DEVELOPER_PIN) is False


@pytest.mark.anyio
async def test_production_replaces_a_published_default_pin(
    clean_developer_user: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DEVELOPER_PIN_ENV, raising=False)
    _use_mode(monkeypatch, OperatingMode.development)
    await main._ensure_developer_user()
    assert _accepts(await _developer_pin_hash(), DEFAULT_DEVELOPER_PIN) is True

    _use_mode(monkeypatch, OperatingMode.production)
    await main._ensure_developer_user()

    assert _accepts(await _developer_pin_hash(), DEFAULT_DEVELOPER_PIN) is False
