import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from openshelf_os_server.database.models import AuditLog
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.utils.audit_constants import AUDIT_RETENTION_DAYS


async def latest_audit_log(endpoint: str, method: str | None = None) -> AuditLog | None:
	async with AsyncSessionLocal() as db:
		query = select(AuditLog).filter(AuditLog.endpoint == endpoint)
		if method is not None:
			query = query.filter(AuditLog.method == method)
		res = await db.execute(query.order_by(AuditLog.id.desc()).limit(1))
		return res.scalar_one_or_none()


async def admin_headers(unauthenticated_client: AsyncClient) -> dict[str, str]:
	response = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "1234"},
	)
	assert response.status_code == 200
	return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.anyio
async def test_audit_chain_integrity_verifies_after_skipped_requests(unauthenticated_client: AsyncClient) -> None:
	headers = await admin_headers(unauthenticated_client)

	first = await unauthenticated_client.get("/api/users", headers=headers)
	skipped_health = await unauthenticated_client.get("/api/health", headers=headers)
	skipped_me = await unauthenticated_client.get("/api/auth/me", headers=headers)
	second = await unauthenticated_client.get("/api/users", headers=headers)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert first.status_code == 200
	assert skipped_health.status_code == 200
	assert skipped_me.status_code == 200
	assert second.status_code == 200
	assert verify.status_code == 200
	assert verify.json()["ok"] is True


@pytest.mark.anyio
async def test_concurrent_requests_keep_one_unbroken_chain(unauthenticated_client: AsyncClient) -> None:
	# A device answers bursts of parallel requests (startup, kiosk login); each must link to
	# the previous entry, or verify reports a chain break nobody caused.
	headers = await admin_headers(unauthenticated_client)

	responses = await asyncio.gather(
		*(unauthenticated_client.get("/api/users", headers=headers) for _ in range(8))
	)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert all(response.status_code == 200 for response in responses)
	assert verify.json()["ok"] is True
	async with AsyncSessionLocal() as db:
		duplicated = await db.scalar(
			select(func.count()).select_from(
				select(AuditLog.seq_no).group_by(AuditLog.seq_no).having(func.count() > 1).subquery()
			)
		)
	assert duplicated == 0


@pytest.mark.anyio
async def test_retention_prune_keeps_clock_skewed_chain_entry(client: AsyncClient) -> None:
	response = await client.get("/api/users")
	assert response.status_code == 200

	async with AsyncSessionLocal() as db:
		anchor_seq = await db.scalar(select(func.max(AuditLog.seq_no)))
		assert anchor_seq is not None
		skewed_seq = int(anchor_seq) + 1
		db.add(
			AuditLog(
				timestamp=datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS + 30),
				method="GET",
				endpoint="/api/skewed",
				response_status=200,
				seq_no=skewed_seq,
				prev_hash="0" * 64,
				entry_hash="1" * 64,
				hmac="2" * 64,
				hmac_key_id="test",
			)
		)
		await db.commit()

	response = await client.get("/api/users")
	assert response.status_code == 200

	async with AsyncSessionLocal() as db:
		still_present = await db.scalar(
			select(func.count()).select_from(AuditLog).where(AuditLog.seq_no == skewed_seq)
		)
	assert still_present == 1


@pytest.mark.anyio
async def test_sensitive_request_fields_are_redacted(unauthenticated_client: AsyncClient) -> None:
	login_response = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "1234"},
	)
	assert login_response.status_code == 200

	login_log = await latest_audit_log("/api/auth/login/json", "POST")
	assert login_log is not None
	assert login_log.request_body == {"email": "admin@opshelf.com", "pin": "[REDACTED]"}

	headers = {"Authorization": f"Bearer {login_response.json()['access_token']}"}
	payload = {
		"name": f"redaction_test_{uuid.uuid4().hex}",
		"permissions": [],
		"password": "secret-password",
		"nested": {"token": "secret-token", "safe": "value"},
		"items": [{"api_key": "secret-key", "hmac_key": "secret-hmac"}],
	}
	role_response = await unauthenticated_client.post("/api/roles", headers=headers, json=payload)
	assert role_response.status_code == 200

	role_log = await latest_audit_log("/api/roles", "POST")
	assert role_log is not None
	assert role_log.request_body == {
		"name": payload["name"],
		"permissions": [],
		"password": "[REDACTED]",
		"nested": {"token": "[REDACTED]", "safe": "value"},
		"items": [{"api_key": "[REDACTED]", "hmac_key": "[REDACTED]"}],
	}
	assert "Bearer" not in str(role_log.request_body)
