import pytest
from sqlalchemy import select
from httpx import AsyncClient
from typing import Any, Dict, Optional

from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.database.models import AuditLog
from openshelf_os_server.utils import audit_integrity
from openshelf_os_server.utils.audit_integrity import build_binding_message, hmac_sha256_hex


async def login(client: AsyncClient, email: str, pin: str) -> str:
	resp = await client.post("/api/auth/login/json", json={"email": email, "pin": pin})
	assert resp.status_code == 200, resp.text
	data = resp.json()
	return str(data["access_token"])


async def _newest_protected_log() -> Optional[Dict[str, Any]]:
	"""Return the scalar fields needed to re-sign the newest protected audit row."""
	async with AsyncSessionLocal() as db:
		result = await db.execute(
			select(AuditLog)
			.where(AuditLog.entry_hash.isnot(None))
			.order_by(AuditLog.seq_no.desc())
			.limit(1)
		)
		log = result.scalar_one_or_none()
		if log is None:
			return None
		return {
			"id": log.id,
			"seq_no": log.seq_no,
			"prev_hash": log.prev_hash,
			"entry_hash": log.entry_hash,
		}


@pytest.mark.anyio
async def test_verify_uses_the_key_id_recorded_on_each_row(
	client: AsyncClient,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A rotated audit key must not make rows signed with the published key look tampered."""
	token = await login(client, "admin@opshelf.com", "1234")
	await client.get("/api/users", headers={"Authorization": f"Bearer {token}"})

	log = await _newest_protected_log()
	assert log is not None and log["seq_no"] is not None
	seq_no = int(log["seq_no"])
	binding = build_binding_message(seq_no, str(log["prev_hash"]), str(log["entry_hash"]))
	published_key = audit_integrity.PUBLISHED_HMAC_KEYS["v1"][0]

	async with AsyncSessionLocal() as db:
		result = await db.execute(select(AuditLog).filter_by(id=log["id"]))
		target = result.scalar_one()
		target.hmac = hmac_sha256_hex(binding, published_key)
		target.hmac_key_id = "v1"
		await db.commit()

	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_CACHE", b"rotated-audit-key-0123456789abcdef")
	monkeypatch.setattr(audit_integrity, "_HMAC_KEY_ID", "v2")

	headers = {"Authorization": f"Bearer {token}"}
	response = await client.get(
		f"/api/audit/verify?from_seq={seq_no}&to_seq={seq_no}",
		headers=headers,
	)
	assert response.status_code == 200
	body = response.json()
	assert body["ok"] is True, body
	assert body["checked"] == 1

	# A row the ring cannot account for is still reported as an integrity failure.
	async with AsyncSessionLocal() as db:
		result = await db.execute(select(AuditLog).filter_by(id=log["id"]))
		target = result.scalar_one()
		target.hmac = hmac_sha256_hex(binding, b"some-key-the-ring-does-not-know")
		await db.commit()

	response = await client.get(
		f"/api/audit/verify?from_seq={seq_no}&to_seq={seq_no}",
		headers=headers,
	)
	body = response.json()
	assert body["ok"] is False
	assert body["first_error_seq"] == seq_no
	assert "HMAC mismatch" in str(body["message"])


def test_auth_service_rejects_empty_secret() -> None:
	"""PyJWT 2.10.1 signs and verifies with an empty HMAC key, so
	AuthService must reject it at construction instead of signing tokens
	with a reproducible all-known key."""
	with pytest.raises(ValueError):
		AuthService("", "HS256")

	auth = AuthService("test-jwt-secret", "HS256")
	token = auth.create_access_token(
		user_id=1,
		email="admin@opshelf.com",
		role="admin",
		permissions=["storage:read"],
	)
	assert auth.decode_token(token) is not None
