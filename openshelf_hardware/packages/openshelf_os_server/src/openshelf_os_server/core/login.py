"""Private operator-login identity, barcode and retry helpers."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from argon2 import PasswordHasher
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database.models import RobotEvent, User
from .auth import AuthService
from .event_log import read_robot_events

_DUMMY_PIN_HASH = PasswordHasher().hash(secrets.token_urlsafe(16))
_MAX_BACKOFF_ENTRIES = 1024
_MAX_BACKOFF_SECONDS = 60
LOGIN_BARCODE_PREFIX = "USR-"
LOGIN_BARCODE_RANDOM_DIGITS = 24
SCAN_TOKEN_TTL_S = 60
SCAN_EVENT_MAX_AGE_S = 120
SCAN_STREAM_POLL_S = 0.25
SCAN_STREAM_PAGE_LIMIT = 500


def credential_backoff_key(value: str) -> str:
    """Return a non-reversible, bounded-size key for one supplied credential."""
    return hashlib.sha256(value.strip().casefold().encode("utf-8")).hexdigest()


class LoginBackoff:
    """Small process-local exponential backoff keyed by supplied credentials."""

    def __init__(self) -> None:
        self._failures: OrderedDict[str, tuple[int, float]] = OrderedDict()

    def retry_after(self, key: str) -> int:
        state = self._failures.get(key)
        if state is None:
            return 0
        remaining = state[1] - time.monotonic()
        return max(0, int(remaining + 0.999))

    def record_failure(self, key: str) -> int:
        previous = self._failures.pop(key, None)
        failure_count = 1 if previous is None else previous[0] + 1
        delay = min(_MAX_BACKOFF_SECONDS, 2 ** (failure_count - 1))
        self._failures[key] = (failure_count, time.monotonic() + delay)
        while len(self._failures) > _MAX_BACKOFF_ENTRIES:
            self._failures.popitem(last=False)
        return delay

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)

    def reset(self) -> None:
        """Clear state for deterministic tests."""
        self._failures.clear()


login_backoff = LoginBackoff()


async def resolve_identifier(db: AsyncSession, identifier: str) -> User | None:
    """Resolve an exact email, unique full name, or assigned login barcode."""
    normalized = identifier.strip()
    if not normalized:
        return None

    active = (
        select(User)
        .options(selectinload(User.role), selectinload(User.access_level))
        .where(User.is_active)
    )
    # An exact email may name a system user: the dev UI signs in as the developer system user
    # by email. Barcode and name matching stay limited to people.
    result = await db.execute(active.where(func.lower(User.email) == normalized.casefold()))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    base = active.where(~User.is_system)

    result = await db.execute(base.where(User.login_barcode == normalized))
    user = result.scalar_one_or_none()
    if user is not None:
        return user

    full_name = func.lower(func.trim(User.first_name + " " + User.last_name))
    result = await db.execute(base.where(full_name == normalized.casefold()))
    users = list(result.scalars().all())
    return users[0] if len(users) == 1 else None


async def resolve_login_barcode(db: AsyncSession, barcode: str) -> User | None:
    result = await db.execute(
        select(User)
        .options(selectinload(User.role), selectinload(User.access_level))
        .where(User.login_barcode == barcode, User.is_active, ~User.is_system)
    )
    return result.scalar_one_or_none()


async def resolve_scan_token(
    db: AsyncSession,
    auth_service: AuthService,
    token: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Resolve a scan token through its recent durable scanner event."""
    seq = auth_service.decode_scan_token(token)
    if seq is None:
        return None
    event = await db.get(RobotEvent, seq)
    if event is None:
        return None
    payload = event.payload if isinstance(event.payload, dict) else {}
    barcode = payload.get("payload")
    if payload.get("kind") != "label_scanned" or not isinstance(barcode, str):
        return None
    created_at = event.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current - created_at > timedelta(seconds=SCAN_EVENT_MAX_AGE_S):
        return None
    return barcode.strip() or None


def scan_frame(event: RobotEvent, auth_service: AuthService) -> Optional[dict[str, Any]]:
    """Build a public token-only frame for one committed scanner event."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    barcode = payload.get("payload")
    if (
        payload.get("kind") != "label_scanned"
        or not isinstance(barcode, str)
        or not barcode.strip()
    ):
        return None
    return {
        "scan_token": auth_service.create_scan_token(event.seq, SCAN_TOKEN_TTL_S),
        "expires_in": SCAN_TOKEN_TTL_S,
    }


async def scan_token_frames(
    auth_service: AuthService,
    start_seq: int,
    *,
    poll_s: float = SCAN_STREAM_POLL_S,
    read: Callable[[int, int], Awaitable[list[RobotEvent]]] = read_robot_events,
) -> AsyncIterator[dict[str, Any]]:
    """Yield token-only frames for scanner events committed after ``start_seq``."""
    cursor = start_seq
    while True:
        rows = await read(cursor, SCAN_STREAM_PAGE_LIMIT)
        for row in rows:
            cursor = row.seq
            frame = scan_frame(row, auth_service)
            if frame is not None:
                yield frame
        if len(rows) < SCAN_STREAM_PAGE_LIMIT:
            await asyncio.sleep(poll_s)


def verify_pin_or_dummy(
    auth_service: AuthService,
    pin: str,
    user: User | None,
) -> bool:
    """Spend one Argon2 verification even when identity resolution failed."""
    return auth_service.verify_pin(pin, user.pin_hash if user is not None else _DUMMY_PIN_HASH)


def generate_login_barcode() -> str:
    """Return a new high-entropy, printable Code 128 login barcode value."""
    digits = "".join(secrets.choice("0123456789") for _ in range(LOGIN_BARCODE_RANDOM_DIGITS))
    return f"{LOGIN_BARCODE_PREFIX}{digits}"
