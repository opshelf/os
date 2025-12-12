import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import select
import json
from httpx import AsyncClient

from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.database.models import User, AuditLog


@pytest.mark.anyio
async def test_audit_logs_without_auth_header(unauthenticated_client: AsyncClient) -> None:
	# Make request without auth header to a public endpoint
	resp = await unauthenticated_client.get("/api/auth/users")
	assert resp.status_code == 200

	# Verify audit log was created without user_id
	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/auth/users")
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one_or_none()
		assert log is not None
		assert log.user_id is None
		assert log.method == "GET"
		assert log.response_status == 200


@pytest.mark.anyio
async def test_audit_logs_with_invalid_auth_token(unauthenticated_client: AsyncClient) -> None:
	headers = {"Authorization": "Bearer invalid_token_here"}

	# Request with invalid token should still be logged
	resp = await unauthenticated_client.get("/api/users", headers=headers)
	assert resp.status_code == 401

	# Verify audit log was created
	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/users")
			.filter(AuditLog.response_status == 401)
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one_or_none()
		assert log is not None
		assert log.user_id is None
		assert log.error_message == ""  # 401 is not an exception


@pytest.mark.anyio
async def test_audit_logs_request_with_body(client: AsyncClient) -> None:
	# Login to get token
	resp = await client.post("/api/auth/login/json", json={"email": "admin@opshelf.com", "pin": "1234"})
	assert resp.status_code == 200
	token = resp.json()["access_token"]
	headers = {"Authorization": f"Bearer {token}"}

	# Make request with JSON body
	test_payload = {"name": "test_role_audit", "permissions": ["test:read"]}
	resp = await client.post("/api/roles", headers=headers, json=test_payload)
	assert resp.status_code == 200

	# Verify request body was logged
	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/roles")
			.filter(AuditLog.method == "POST")
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one()
		assert log.request_body == test_payload
		assert log.response_status == 200


@pytest.mark.anyio
async def test_audit_logs_different_methods(client: AsyncClient) -> None:
	# Login
	resp = await client.post("/api/auth/login/json", json={"email": "admin@opshelf.com", "pin": "1234"})
	token = resp.json()["access_token"]
	headers = {"Authorization": f"Bearer {token}"}

	# Test different HTTP methods
	methods_to_test = [
		("GET", "/api/users", None),
		("POST", "/api/roles", {"name": "method_test", "permissions": []}),
	]

	for method, endpoint, body in methods_to_test:
		if method == "GET":
			resp = await client.get(endpoint, headers=headers)
		elif method == "POST":
			resp = await client.post(endpoint, headers=headers, json=body)
		
		assert resp.status_code in [200, 201]

	# Verify all methods were logged correctly
	async with AsyncSessionLocal() as db:
		for method, endpoint, _ in methods_to_test:
			res = await db.execute(
				select(AuditLog)
				.filter(AuditLog.endpoint == endpoint)
				.filter(AuditLog.method == method)
				.order_by(AuditLog.id.desc())
				.limit(1)
			)
			log = res.scalar_one_or_none()
			assert log is not None
			assert log.method == method


@pytest.mark.anyio
async def test_audit_logs_client_info(unauthenticated_client: AsyncClient) -> None:
	# Custom headers to test user agent and IP tracking
	headers = {
		"User-Agent": "TestClient/1.0",
	}

	resp = await unauthenticated_client.get("/api/auth/users", headers=headers)
	assert resp.status_code == 200

	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/auth/users")
			.filter(AuditLog.user_agent == "TestClient/1.0")
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one()
		assert log.user_agent == "TestClient/1.0"
		assert log.ip_address in ["testclient", "127.0.0.1"]  # httpx test client may use either


@pytest.mark.anyio
async def test_audit_integrity_fields_populated(client: AsyncClient) -> None:
	# Login and make a request
	resp = await client.post("/api/auth/login/json", json={"email": "admin@opshelf.com", "pin": "1234"})
	token = resp.json()["access_token"]
	headers = {"Authorization": f"Bearer {token}"}
	
	await client.get("/api/users", headers=headers)

	# Check that integrity fields are populated
	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/users")
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one()
		
		# Integrity fields should be populated
		assert log.seq_no is not None
		assert log.prev_hash is not None
		assert len(log.prev_hash) == 64  # SHA-256 hex
		assert log.entry_hash is not None
		assert len(log.entry_hash) == 64
		assert log.hmac is not None
		assert len(log.hmac) == 64
		assert log.hmac_key_id is not None


@pytest.mark.anyio
async def test_audit_chain_continuity(client: AsyncClient) -> None:
	# Login
	resp = await client.post("/api/auth/login/json", json={"email": "admin@opshelf.com", "pin": "1234"})
	token = resp.json()["access_token"]
	headers = {"Authorization": f"Bearer {token}"}

	# Make multiple requests to build a chain
	for i in range(3):
		await client.get("/api/auth/me", headers=headers)

	# Verify chain continuity
	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.entry_hash.isnot(None))
			.order_by(AuditLog.seq_no.asc())
			.limit(10)
		)
		logs = res.scalars().all()

		prev_hash = "0" * 64  # Initial state
		prev_seq = 0
		
		for log in logs:
			if log.seq_no > prev_seq + 1:
				# Allow gaps but prev_hash should still match
				pass
			
			if prev_seq > 0:
				# Each log should reference the previous one's hash
				assert log.prev_hash == prev_hash or log.seq_no == 1
			
			prev_hash = log.entry_hash
			prev_seq = log.seq_no


@pytest.mark.anyio
async def test_audit_logs_errors_in_endpoint(client: AsyncClient) -> None:
	# Test that errors in endpoints are logged
	resp = await client.post("/api/auth/login/json", json={"email": "admin@opshelf.com", "pin": "1234"})
	token = resp.json()["access_token"]
	headers = {"Authorization": f"Bearer {token}"}

	# Try to get non-existent user
	resp = await client.get("/api/users/999999", headers=headers)
	assert resp.status_code == 404

	# Verify error was logged
	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/users/999999")
			.filter(AuditLog.response_status == 404)
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one_or_none()
		assert log is not None
		assert log.response_status == 404


@pytest.mark.anyio
async def test_audit_duration_tracking(unauthenticated_client: AsyncClient) -> None:
	# Make a request and verify duration is tracked
	resp = await unauthenticated_client.get("/api/auth/users")
	assert resp.status_code == 200

	async with AsyncSessionLocal() as db:
		res = await db.execute(
			select(AuditLog)
			.filter(AuditLog.endpoint == "/api/auth/users")
			.order_by(AuditLog.id.desc())
			.limit(1)
		)
		log = res.scalar_one()
		assert log.request_duration_ms is not None
		assert log.request_duration_ms >= 0