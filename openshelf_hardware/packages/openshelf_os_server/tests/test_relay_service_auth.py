from __future__ import annotations

import hashlib

import httpx
import pytest

from openshelf_os_server.main import _ensure_developer_user
from openshelf_os_server.routers.auth_deps import RELAY_SERVICE_TOKEN_SHA256_ENV


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
    assert command.json()["detail"] == "Robot command not found"

    printed = await unauthenticated_client.post(
        "/api/print", json={"barcode": "036000291452"}, headers=headers
    )
    assert printed.status_code == 200

    users = await unauthenticated_client.get("/api/users", headers=headers)
    assert users.status_code == 401

    locations = await unauthenticated_client.post(
        "/api/storage/items/locations/remove",
        headers=headers,
        json={"barcode": "not-found", "side": "left_module", "shelf_idx": 0, "cell_idx": 0},
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
