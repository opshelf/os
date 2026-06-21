import sqlite3

import pytest
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from openshelf_os_server.database import session as session_module
from openshelf_os_server.database.session import get_db


class FakeSession:
    def __init__(self, *, rollback_error: Exception | None = None, close_error: Exception | None = None) -> None:
        self.rollback_error = rollback_error
        self.close_error = close_error
        self.rollback_called = False
        self.close_called = False

    def in_transaction(self) -> bool:
        return True

    async def rollback(self) -> None:
        self.rollback_called = True
        if self.rollback_error is not None:
            raise self.rollback_error

    async def close(self) -> None:
        self.close_called = True
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.anyio
async def test_get_db_preserves_endpoint_exception_when_sqlite_rollback_connection_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakeSession(rollback_error=ValueError("no active connection"))
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: fake_session)

    db = get_db()
    assert await anext(db) is fake_session

    original_error = RuntimeError("endpoint failed")
    with pytest.raises(RuntimeError, match="endpoint failed"):
        await db.athrow(original_error)

    assert fake_session.rollback_called is True
    assert fake_session.close_called is True


@pytest.mark.anyio
async def test_get_db_ignores_closed_sqlite_connection_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_session = FakeSession(close_error=ValueError("no active connection"))
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: fake_session)

    db = get_db()
    assert await anext(db) is fake_session

    await db.aclose()

    assert fake_session.close_called is True


@pytest.mark.anyio
async def test_get_db_ignores_sqlalchemy_wrapped_closed_sqlite_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_error = SQLAlchemyOperationalError(
        "ROLLBACK",
        {},
        sqlite3.OperationalError("no active connection"),
    )
    fake_session = FakeSession(close_error=cleanup_error)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: fake_session)

    db = get_db()
    assert await anext(db) is fake_session

    await db.aclose()

    assert fake_session.close_called is True


@pytest.mark.anyio
async def test_get_db_reraises_unexpected_cleanup_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = FakeSession(close_error=ValueError("different failure"))
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: fake_session)

    db = get_db()
    assert await anext(db) is fake_session

    with pytest.raises(ValueError, match="different failure"):
        await db.aclose()

    assert fake_session.close_called is True
