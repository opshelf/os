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
from openshelf_os_server.settings import (
    DEFAULT_DEVELOPER_PIN,
    DEVELOPER_PIN_ENV,
    DEVELOPER_SYSTEM_USER_EMAIL,
    configured_developer_pin,
)


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


async def _set_developer_pin(pin: str) -> None:
    async with AsyncSessionLocal() as db:
        user = (
            await db.execute(select(User).where(User.email == DEVELOPER_SYSTEM_USER_EMAIL))
        ).scalar_one()
        user.pin_hash = PasswordHasher().hash(pin)
        await db.commit()


def _accepts(pin_hash: str, pin: str) -> bool:
    return main._pin_matches(PasswordHasher(), pin_hash, pin)


def _use_mode(monkeypatch: pytest.MonkeyPatch, mode: OperatingMode) -> None:
    monkeypatch.setattr(main, "get_operating_mode", lambda: mode)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (OperatingMode.development, DEFAULT_DEVELOPER_PIN),
        (OperatingMode.mock, DEFAULT_DEVELOPER_PIN),
        (OperatingMode.demo, None),
        (OperatingMode.production, None),
    ],
)
def test_default_pin_only_in_development_and_mock(
    monkeypatch: pytest.MonkeyPatch, mode: OperatingMode, expected: str | None
) -> None:
    monkeypatch.delenv(DEVELOPER_PIN_ENV, raising=False)
    assert configured_developer_pin(mode) == expected


@pytest.mark.parametrize("mode", list(OperatingMode))
def test_configured_pin_wins_in_every_mode(monkeypatch: pytest.MonkeyPatch, mode: OperatingMode) -> None:
    monkeypatch.setenv(DEVELOPER_PIN_ENV, "482916")
    assert configured_developer_pin(mode) == "482916"


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


@pytest.mark.anyio
async def test_production_keeps_a_pin_set_by_hand_when_none_is_configured(
    clean_developer_user: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DEVELOPER_PIN_ENV, raising=False)
    _use_mode(monkeypatch, OperatingMode.production)
    await main._ensure_developer_user()
    await _set_developer_pin("905173")

    await main._ensure_developer_user()

    assert _accepts(await _developer_pin_hash(), "905173") is True


@pytest.mark.anyio
async def test_configured_pin_replaces_any_other_pin(
    clean_developer_user: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_mode(monkeypatch, OperatingMode.production)
    monkeypatch.delenv(DEVELOPER_PIN_ENV, raising=False)
    await main._ensure_developer_user()

    monkeypatch.setenv(DEVELOPER_PIN_ENV, "482916")
    await main._ensure_developer_user()
    pin_hash = await _developer_pin_hash()

    assert _accepts(pin_hash, "482916") is True
    await main._ensure_developer_user()
    assert await _developer_pin_hash() == pin_hash


@pytest.mark.anyio
async def test_development_restores_the_default_pin(
    clean_developer_user: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DEVELOPER_PIN_ENV, raising=False)
    _use_mode(monkeypatch, OperatingMode.production)
    await main._ensure_developer_user()

    _use_mode(monkeypatch, OperatingMode.development)
    await main._ensure_developer_user()

    assert _accepts(await _developer_pin_hash(), DEFAULT_DEVELOPER_PIN) is True
