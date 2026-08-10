import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from starlette.requests import Request

from openshelf_os_server.database.models import AuditLog
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.main import app
from openshelf_os_server.middleware.audit import (
	AuditMiddleware,
	should_skip_after_response,
	should_skip_before_request,
)
from openshelf_os_server.utils.audit_constants import AUDIT_RETENTION_DAYS


def _make_request(method: str, path: str) -> Request:
	scope = {
		"type": "http",
		"method": method,
		"path": path,
		"headers": [],
		"query_string": b"",
	}
	return Request(scope)


async def audit_count(endpoint: str | None = None, method: str | None = None) -> int:
	async with AsyncSessionLocal() as db:
		query = select(func.count()).select_from(AuditLog)
		if endpoint is not None:
			query = query.filter(AuditLog.endpoint == endpoint)
		if method is not None:
			query = query.filter(AuditLog.method == method)
		return int(await db.scalar(query) or 0)


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
async def test_audit_logs_without_auth_header(unauthenticated_client: AsyncClient) -> None:
	response = await unauthenticated_client.post(
		"/api/auth/login/json",
		json={"email": "admin@opshelf.com", "pin": "1234"},
	)
	assert response.status_code == 200

	log = await latest_audit_log("/api/auth/login/json", "POST")
	assert log is not None
	assert log.user_id is None
	assert log.method == "POST"
	assert log.response_status == 200


@pytest.mark.anyio
async def test_audit_logs_with_invalid_auth_token(unauthenticated_client: AsyncClient) -> None:
	headers = {"Authorization": "Bearer invalid_token_here"}

	response = await unauthenticated_client.get("/api/users", headers=headers)
	assert response.status_code == 401

	log = await latest_audit_log("/api/users", "GET")
	assert log is not None
	assert log.user_id is None
	assert log.response_status == 401
	assert log.error_message == ""


@pytest.mark.anyio
async def test_audit_logs_request_with_body(unauthenticated_client: AsyncClient) -> None:
	headers = await admin_headers(unauthenticated_client)
	test_payload = {"name": f"test_role_audit_{uuid.uuid4().hex}", "permissions": ["test:read"]}

	response = await unauthenticated_client.post("/api/roles", headers=headers, json=test_payload)
	assert response.status_code == 201

	log = await latest_audit_log("/api/roles", "POST")
	assert log is not None
	assert log.request_body == test_payload
	assert log.response_status == 201


@pytest.mark.anyio
async def test_audit_logs_different_methods(unauthenticated_client: AsyncClient) -> None:
	headers = await admin_headers(unauthenticated_client)
	role_payload = {"name": f"method_test_{uuid.uuid4().hex}", "permissions": []}

	get_response = await unauthenticated_client.get("/api/users", headers=headers)
	post_response = await unauthenticated_client.post("/api/roles", headers=headers, json=role_payload)

	assert get_response.status_code == 200
	assert post_response.status_code == 201
	assert await latest_audit_log("/api/users", "GET") is not None
	assert await latest_audit_log("/api/roles", "POST") is not None


@pytest.mark.anyio
async def test_audit_logs_client_info(unauthenticated_client: AsyncClient) -> None:
	headers = {"User-Agent": "TestClient/1.0"}
	response = await unauthenticated_client.post(
		"/api/auth/login/json",
		headers=headers,
		json={"email": "admin@opshelf.com", "pin": "1234"},
	)
	assert response.status_code == 200

	log = await latest_audit_log("/api/auth/login/json", "POST")
	assert log is not None
	assert log.user_agent == "TestClient/1.0"
	assert log.ip_address in ["testclient", "127.0.0.1"]


@pytest.mark.anyio
async def test_audit_integrity_fields_populated(client: AsyncClient) -> None:
	await client.get("/api/users")

	log = await latest_audit_log("/api/users", "GET")
	assert log is not None
	assert log.seq_no is not None
	assert log.prev_hash is not None
	assert len(log.prev_hash) == 64
	assert log.entry_hash is not None
	assert len(log.entry_hash) == 64
	assert log.hmac is not None
	assert len(log.hmac) == 64
	assert log.hmac_key_id is not None


@pytest.mark.anyio
async def test_audit_integrity_verifies_entries_without_client_metadata(
	unauthenticated_client: AsyncClient,
) -> None:
	endpoint = f"/api/audit-no-client-metadata/{uuid.uuid4().hex}"
	await AuditMiddleware(app)._log_request(
		request=_make_request("POST", endpoint),
		request_body={"source": "test"},
		response_status=200,
		response_body={"ok": True},
		error_message="",
		duration_ms=1,
	)

	log = await latest_audit_log(endpoint, "POST")
	assert log is not None
	assert log.ip_address is None
	assert log.user_agent is None

	headers = await admin_headers(unauthenticated_client)
	verify = await unauthenticated_client.get("/api/audit/verify", headers=headers)
	assert verify.status_code == 200
	assert verify.json()["checked"] == 2
	assert verify.json()["ok"] is True


def test_concurrent_audit_writes_across_event_loops_form_one_linear_chain() -> None:
	prefix = f"/api/concurrent-audit/{uuid.uuid4().hex}"
	middleware = AuditMiddleware(app)
	wave_size = 12

	async def write_wave(wave: int) -> None:
		start = asyncio.Event()

		async def write(index: int) -> None:
			await start.wait()
			await middleware._log_request(
				request=_make_request("POST", f"{prefix}/{wave}/{index}"),
				request_body={"wave": wave, "index": index},
				response_status=200,
				response_body={"ok": True},
				error_message="",
				duration_ms=1,
			)

		tasks = [asyncio.create_task(write(index)) for index in range(wave_size)]
		await asyncio.sleep(0)
		start.set()
		await asyncio.gather(*tasks)

	async def fetch_logs() -> list[AuditLog]:
		async with AsyncSessionLocal() as db:
			result = await db.execute(
				select(AuditLog)
				.where(AuditLog.endpoint.like(f"{prefix}/%"))
				.order_by(AuditLog.seq_no.asc())
			)
			return list(result.scalars().all())

	asyncio.run(write_wave(1))
	asyncio.run(write_wave(2))
	logs = asyncio.run(fetch_logs())

	assert len(logs) == wave_size * 2
	assert len({log.seq_no for log in logs}) == len(logs)
	for previous, current in zip(logs, logs[1:]):
		assert current.seq_no == previous.seq_no + 1
		assert current.prev_hash == previous.entry_hash


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
async def test_audit_logs_errors_in_endpoint(client: AsyncClient) -> None:
	response = await client.get("/api/users/999999")
	assert response.status_code == 404

	log = await latest_audit_log("/api/users/999999", "GET")
	assert log is not None
	assert log.response_status == 404


@pytest.mark.anyio
async def test_audit_duration_tracking(client: AsyncClient) -> None:
	response = await client.get("/api/users")
	assert response.status_code == 200

	log = await latest_audit_log("/api/users", "GET")
	assert log is not None
	assert log.request_duration_ms is not None
	assert log.request_duration_ms >= 0


@pytest.mark.anyio
async def test_audit_error_response_keeps_cors_header(unauthenticated_client: AsyncClient) -> None:
	path = "/api/test/audit-error"

	if all(getattr(route, "path", None) != path for route in app.routes):
		@app.get(path)
		async def audit_error_route() -> None:
			raise RuntimeError("audit error route failed")

	response = await unauthenticated_client.get(path, headers={"Origin": "http://dev-ui.test"})

	assert response.status_code == 500
	assert response.headers["access-control-allow-origin"] == "*"
	assert response.json()["detail"]["code"] == "internal_error"

	log = await latest_audit_log(path, "GET")
	assert log is not None
	assert log.response_status == 500
	assert log.error_message == "audit error route failed"


@pytest.mark.anyio
async def test_options_requests_are_not_audited(unauthenticated_client: AsyncClient) -> None:
	before_count = await audit_count(method="OPTIONS")
	response = await unauthenticated_client.options(
		"/api/auth/users",
		headers={"Origin": "http://dev-ui.test", "Access-Control-Request-Method": "GET"},
	)

	assert response.status_code < 500
	assert await audit_count(method="OPTIONS") == before_count


@pytest.mark.anyio
async def test_health_requests_are_not_audited(unauthenticated_client: AsyncClient) -> None:
	before_count = await audit_count(endpoint="/api/health")
	response = await unauthenticated_client.get("/api/health")

	assert response.status_code == 200
	assert await audit_count(endpoint="/api/health") == before_count


@pytest.mark.anyio
async def test_audit_write_prunes_unprotected_entries_older_than_retention(client: AsyncClient) -> None:
	stale_endpoint = f"/api/stale/{uuid.uuid4().hex}"
	async with AsyncSessionLocal() as db:
		db.add(
			AuditLog(
				timestamp=datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS + 1),
				method="GET",
				endpoint=stale_endpoint,
				response_status=200,
			)
		)
		await db.commit()

	assert await audit_count(endpoint=stale_endpoint) == 1

	response = await client.get("/api/users")
	assert response.status_code == 200

	assert await audit_count(endpoint=stale_endpoint) == 0


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


@pytest.mark.parametrize(
	"method, path",
	[
		("POST", "/api/robot/debug/motors/probe"),
		("GET", "/api/calibration/laser/stream"),
		("DELETE", "/api/robot/debug/motors/probe"),
	],
)
def test_diagnostic_endpoints_skipped_before_request(method: str, path: str) -> None:
	request = _make_request(method, path)
	assert should_skip_before_request(request) is True


def test_laser_status_successful_read_is_excluded() -> None:
	request = _make_request("GET", "/api/calibration/laser/status")
	assert should_skip_before_request(request) is False
	assert should_skip_after_response(request, 200) is True
	assert should_skip_after_response(request, 500) is False


@pytest.mark.anyio
async def test_successful_excluded_get_is_not_audited(client: AsyncClient) -> None:
	before_count = await audit_count(endpoint="/api/auth/me", method="GET")
	response = await client.get("/api/auth/me")

	assert response.status_code == 200
	assert await audit_count(endpoint="/api/auth/me", method="GET") == before_count


@pytest.mark.anyio
async def test_failed_excluded_get_is_audited(client: AsyncClient) -> None:
	response = await client.get("/api/storage/items/missing-upca")
	assert response.status_code == 404

	log = await latest_audit_log("/api/storage/items/missing-upca", "GET")
	assert log is not None
	assert log.response_status == 404


@pytest.mark.anyio
async def test_successful_storage_mutation_is_audited(client: AsyncClient) -> None:
	payload = {
		"upca": f"{uuid.uuid4().int % 10**12:012d}",
		"name": "Audit Test Item",
		"dimensions": {"width": 1.0, "height": 2.0, "length": 3.0},
		"weight": 4.0,
	}
	response = await client.post("/api/storage/items", json=payload)
	assert response.status_code == 201

	log = await latest_audit_log("/api/storage/items", "POST")
	assert log is not None
	assert log.response_status == 201
	assert log.request_body == payload


@pytest.mark.anyio
async def test_robot_command_request_is_audited(client: AsyncClient) -> None:
	payload = {"command": "home"}
	response = await client.post("/api/robot/commands", json=payload)
	assert response.status_code in [200, 422, 503]

	log = await latest_audit_log("/api/robot/commands", "POST")
	assert log is not None
	assert log.response_status == response.status_code
	assert log.request_body == payload


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
	assert role_response.status_code == 201

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
