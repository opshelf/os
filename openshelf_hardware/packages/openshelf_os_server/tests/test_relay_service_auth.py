from __future__ import annotations

import hashlib
import logging

import httpx
import pytest

from openshelf_os_server.main import _ensure_developer_user
from openshelf_os_server.routers import auth_deps
from openshelf_os_server.routers.auth_deps import (
    RELAY_SERVICE_TOKEN_SHA256_ENV,
    report_relay_service_token_configuration,
)


RELAY_SERVICE_TOKEN = "os_service.test-relay-token-with-at-least-32-characters"


@pytest.mark.asyncio
async def test_relay_service_token_is_limited_to_relay_routes(
    unauthenticated_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _ensure_developer_user()
    monkeypatch.setenv(
        RELAY_SERVICE_TOKEN_SHA256_ENV,
        hashlib.sha256(RELAY_SERVICE_TOKEN.encode("utf-8")).hexdigest(),
    )
    headers = {"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"}

    items = await unauthenticated_client.get("/api/storage/items", headers=headers)
    assert items.status_code == 200

    command = await unauthenticated_client.get(
        "/api/robot/commands/nonexistent", headers=headers
    )
    assert command.status_code == 404
    assert command.json()["detail"]["message"] == "That robot command does not exist."

    printed = await unauthenticated_client.post(
        "/api/print", json={"barcode": "036000291452"}, headers=headers
    )
    assert printed.status_code == 200

    users = await unauthenticated_client.get("/api/users", headers=headers)
    assert users.status_code == 401

    locations = await unauthenticated_client.post(
        "/api/storage/items/locations/remove",
        headers=headers,
        json={"barcode": "not-found", "side": "front_module", "shelf_idx": 0, "cell_idx": 0},
    )
    assert locations.status_code == 401

    clear_commands = await unauthenticated_client.post(
        "/api/robot/clear_commands", headers=headers
    )
    assert clear_commands.status_code == 401


@pytest.mark.asyncio
async def test_relay_service_token_rejects_wrong_missing_and_query_credentials(
    unauthenticated_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = hashlib.sha256(RELAY_SERVICE_TOKEN.encode("utf-8")).hexdigest()
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, digest)

    wrong = await unauthenticated_client.get(
        "/api/storage/items", headers={"Authorization": "Bearer wrong-token"}
    )
    assert wrong.status_code == 401

    query = await unauthenticated_client.get(
        "/api/storage/items", params={"token": RELAY_SERVICE_TOKEN}
    )
    assert query.status_code == 401

    monkeypatch.delenv(RELAY_SERVICE_TOKEN_SHA256_ENV)
    missing = await unauthenticated_client.get(
        "/api/storage/items",
        headers={"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"},
    )
    assert missing.status_code == 401


RELAY_DIGEST = hashlib.sha256(RELAY_SERVICE_TOKEN.encode("utf-8")).hexdigest()


@pytest.fixture
def fresh_rejection_log(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_deps, "_relay_rejection_last_logged", {})


def _rejections(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == "relay_service_token_rejected"]


def _assert_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    for record in caplog.records:
        rendered = f"{record.getMessage()} {record.__dict__}"
        assert RELAY_SERVICE_TOKEN not in rendered
        assert RELAY_DIGEST not in rendered


@pytest.mark.parametrize(
    ("value", "reason"),
    [(None, "hash_not_configured"), ("", "hash_not_configured"), (RELAY_DIGEST.upper(), "hash_malformed"), ("abc", "hash_malformed")],
)
def test_startup_warns_when_relay_digest_is_unusable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None, reason: str
) -> None:
    if value is None:
        monkeypatch.delenv(RELAY_SERVICE_TOKEN_SHA256_ENV, raising=False)
    else:
        monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, value)

    with caplog.at_level(logging.WARNING):
        report_relay_service_token_configuration()

    records = [r for r in caplog.records if r.getMessage() == "relay_service_token_hash_unusable"]
    assert len(records) == 1
    assert records[0].reason == reason
    assert "refused" in records[0].impact
    if value:
        assert all(value not in f"{r.__dict__}" for r in caplog.records)


def test_startup_is_quiet_when_relay_digest_is_valid(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, RELAY_DIGEST)
    with caplog.at_level(logging.WARNING):
        report_relay_service_token_configuration()
    assert not [r for r in caplog.records if r.getMessage() == "relay_service_token_hash_unusable"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("digest", "reason"),
    [
        ("", "hash_not_configured"),
        ("not-a-digest", "hash_malformed"),
        (hashlib.sha256(b"os_service.some-other-token").hexdigest(), "mismatch"),
    ],
)
async def test_rejected_relay_token_logs_reason_and_correlation_id(
    unauthenticated_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fresh_rejection_log: None,
    digest: str,
    reason: str,
) -> None:
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, digest)

    with caplog.at_level(logging.WARNING):
        response = await unauthenticated_client.get(
            "/api/storage/items",
            headers={"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}", "X-Correlation-ID": "corr-relay-1"},
        )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "token_invalid"
    records = _rejections(caplog)
    assert len(records) == 1
    assert records[0].reason == reason
    assert records[0].correlation_id == "corr-relay-1"
    _assert_no_secrets(caplog)


@pytest.mark.asyncio
async def test_relay_rejection_log_is_rate_limited_per_reason(
    unauthenticated_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fresh_rejection_log: None,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(auth_deps.time, "monotonic", lambda: clock[0])
    headers = {"Authorization": f"Bearer {RELAY_SERVICE_TOKEN}"}

    async def poll() -> None:
        response = await unauthenticated_client.get("/api/storage/items", headers=headers)
        assert response.status_code == 401

    with caplog.at_level(logging.WARNING):
        monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, "")
        await poll()
        await poll()
        assert [r.reason for r in _rejections(caplog)] == ["hash_not_configured"]

        # A different reason is not held back by the first one's window.
        monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, "0" * 64)
        await poll()
        assert [r.reason for r in _rejections(caplog)] == ["hash_not_configured", "mismatch"]

        monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, "")
        clock[0] += auth_deps.RELAY_REJECTION_LOG_INTERVAL_SECONDS
        await poll()
        assert [r.reason for r in _rejections(caplog)] == ["hash_not_configured", "mismatch", "hash_not_configured"]


@pytest.mark.asyncio
async def test_non_relay_tokens_do_not_log_relay_rejections(
    unauthenticated_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fresh_rejection_log: None,
) -> None:
    monkeypatch.setenv(RELAY_SERVICE_TOKEN_SHA256_ENV, "")
    with caplog.at_level(logging.WARNING):
        user_token = await unauthenticated_client.get(
            "/api/storage/items", headers={"Authorization": "Bearer not-a-relay-token"}
        )
        query = await unauthenticated_client.get(
            "/api/storage/items", params={"token": RELAY_SERVICE_TOKEN}
        )
    assert user_token.status_code == 401
    assert query.status_code == 401
    assert _rejections(caplog) == []
