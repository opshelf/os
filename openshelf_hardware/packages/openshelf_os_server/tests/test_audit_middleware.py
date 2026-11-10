import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from openshelf_os_server.database.models import AuditLog
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.utils.audit_constants import AUDIT_RETENTION_DAYS, LEGACY_COMMAND_RECORDS_FIELD
from openshelf_os_server.utils.audit_integrity import (
	build_binding_message,
	canonicalize_payload,
	hmac_sha256_hex,
	sha256_hex,
)


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


@pytest.mark.anyio
async def test_unusual_request_bodies_are_recorded_and_stay_readable(
	unauthenticated_client: AsyncClient,
) -> None:
	# The sign-in route is open to anyone. A JSON list body made the audit page holding its
	# row unreadable, and an escaped lone surrogate dropped the row of a change that was made.
	headers = await admin_headers(unauthenticated_client)
	role_name = f"audit_body_test_{uuid.uuid4().hex}"

	list_body = await unauthenticated_client.post("/api/auth/login/json", json=[1, 2])
	surrogate_body = await unauthenticated_client.post(
		"/api/roles",
		headers={**headers, "Content-Type": "application/json"},
		content=('{"name": "%s", "permissions": [], "note": "a\\ud800b"}' % role_name).encode(),
	)
	page = await unauthenticated_client.get("/api/audit/logs", headers=headers)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert list_body.status_code == 422
	assert surrogate_body.status_code == 200
	list_log = await latest_audit_log("/api/auth/login/json", "POST")
	assert list_log is not None and list_log.request_body == [1, 2]
	role_log = await latest_audit_log("/api/roles", "POST")
	assert role_log is not None
	assert role_log.request_body == {"name": role_name, "permissions": [], "note": "a?b"}
	assert page.status_code == 200
	assert [1, 2] in [entry["request_body"] for entry in page.json()["logs"]]
	assert verify.json()["ok"] is True


@pytest.mark.anyio
async def test_request_without_user_agent_keeps_the_chain_verifiable(
	unauthenticated_client: AsyncClient,
) -> None:
	# The entry hash covered a null user agent while the row stored "", so one such request
	# made verify report tampering from that entry on.
	headers = await admin_headers(unauthenticated_client)
	request = unauthenticated_client.build_request("GET", "/api/users", headers=headers)
	del request.headers["user-agent"]

	response = await unauthenticated_client.send(request)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)

	assert response.status_code == 200
	log = await latest_audit_log("/api/users", "GET")
	assert log is not None and log.user_agent == ""
	assert verify.json()["ok"] is True

	# An entry an older release wrote that way is intact and verifies as well.
	async with AsyncSessionLocal() as db:
		legacy = (await db.execute(select(AuditLog).filter_by(id=log.id))).scalar_one()
		payload = {
			"timestamp": legacy.timestamp.isoformat(),
			"method": legacy.method,
			"endpoint": legacy.endpoint,
			"request_body": legacy.request_body,
			"response_status": legacy.response_status,
			"response_body": legacy.response_body,
			LEGACY_COMMAND_RECORDS_FIELD: legacy.command_records,
			"error_message": legacy.error_message,
			"ip_address": legacy.ip_address,
			"user_agent": None,
			"request_duration_ms": legacy.request_duration_ms,
			"user_id": legacy.user_id,
			"seq_no": legacy.seq_no,
			"prev_hash": legacy.prev_hash,
		}
		legacy.entry_hash = sha256_hex(canonicalize_payload(payload))
		legacy.hmac = hmac_sha256_hex(
			build_binding_message(int(legacy.seq_no), str(legacy.prev_hash), legacy.entry_hash)
		)
		seq_no = int(legacy.seq_no)
		await db.commit()

	legacy_verify = await unauthenticated_client.get(
		f"/api/audit/verify?from_seq={seq_no}&to_seq={seq_no}", headers=headers
	)
	assert legacy_verify.json() == {"ok": True, "checked": 1, "first_error_seq": None, "message": None}
