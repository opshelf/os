import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from httpx import AsyncClient

from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.database.models import User, Role, AuditLog


async def login(client: AsyncClient, email: str, pin: str) -> str:
	resp = await client.post("/api/auth/login/json", json={"email": email, "pin": pin})
	assert resp.status_code == 200, resp.text
	data = resp.json()
	return str(data["access_token"])


@pytest.mark.anyio
async def test_audit_list_with_filters(client: AsyncClient) -> None:
	token = await login(client, "admin@opshelf.com", "1234")
	headers = {"Authorization": f"Bearer {token}"}

	# Generate some audit logs with different endpoints and methods
	await client.get("/api/auth/me", headers=headers)
	await client.get("/api/users", headers=headers)
	await client.post("/api/roles", headers=headers, json={"name": "test_role", "permissions": []})
	await client.get("/api/audit/logs", headers=headers)

	# Test filtering by method
	resp = await client.get("/api/audit/logs?method=GET", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	assert data["total"] > 0
	for log in data["logs"]:
		assert log["method"] == "GET"

	# Test filtering by endpoint
	resp = await client.get("/api/audit/logs?endpoint=/api/users", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	assert data["total"] > 0
	for log in data["logs"]:
		assert "/api/users" in log["endpoint"]

	# Test filtering by date range
	now = datetime.now(timezone.utc)
	date_from = (now - timedelta(minutes=1)).isoformat().replace('+', '%2B')
	date_to = now.isoformat().replace('+', '%2B')
	
	resp = await client.get(f"/api/audit/logs?date_from={date_from}&date_to={date_to}", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	assert data["total"] > 0

	# Test pagination
	resp = await client.get("/api/audit/logs?limit=2&offset=0", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	assert len(data["logs"]) <= 2
	assert data["limit"] == 2
	assert data["offset"] == 0

	# Test combined filters
	resp = await client.get("/api/audit/logs?method=GET&endpoint=/api/auth", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	for log in data["logs"]:
		assert log["method"] == "GET"
		assert "/api/auth" in log["endpoint"]


@pytest.mark.anyio
async def test_audit_list_filter_by_user_id(
	client: AsyncClient,
	unauthenticated_client: AsyncClient,
) -> None:
	admin_token = await login(unauthenticated_client, "admin@opshelf.com", "1234")
	admin_headers = {"Authorization": f"Bearer {admin_token}"}

	# Get admin user id
	me_resp = await client.get("/api/auth/me", headers=admin_headers)
	admin_id = me_resp.json()["id"]
	admin_users_response = await unauthenticated_client.get("/api/users", headers=admin_headers)
	assert admin_users_response.status_code == 200

	# Create another user and generate logs
	async with AsyncSessionLocal() as db:
		res = await db.execute(select(Role).filter(Role.name == "operator"))
		operator_role = res.scalar_one()
		
		from openshelf_os_server.core.auth import AuthService
		from openshelf_os_server.settings import get_settings
		
		settings = get_settings()
		auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
		
		test_user = User(
			email="test_filter@opshelf.com",
			first_name="Test",
			last_name="Filter",
			pin_hash=auth.hash_pin("9999"),
			role_id=operator_role.id,
			is_active=True,
		)
		db.add(test_user)
		await db.commit()
		await db.refresh(test_user)
		test_user_id = test_user.id

	# Generate logs as test user
	test_token = await login(unauthenticated_client, "test_filter@opshelf.com", "9999")
	test_headers = {"Authorization": f"Bearer {test_token}"}
	users_response = await unauthenticated_client.get("/api/users", headers=test_headers)
	assert users_response.status_code == 200

	# Filter by admin user id
	resp = await client.get(f"/api/audit/logs?user_id={admin_id}", headers=admin_headers)
	assert resp.status_code == 200
	data = resp.json()
	assert data["total"] > 0
	for log in data["logs"]:
		assert log["user_id"] == admin_id

	# Filter by test user id
	resp = await client.get(f"/api/audit/logs?user_id={test_user_id}", headers=admin_headers)
	assert resp.status_code == 200
	data = resp.json()
	assert data["total"] > 0
	for log in data["logs"]:
		assert log["user_id"] == test_user_id


@pytest.mark.anyio
async def test_audit_list_sorting(client: AsyncClient) -> None:
	token = await login(client, "admin@opshelf.com", "1234")
	headers = {"Authorization": f"Bearer {token}"}

	# Generate some logs
	for _ in range(3):
		await client.get("/api/auth/me", headers=headers)

	# Get logs sorted by timestamp desc (default)
	resp = await client.get("/api/audit/logs?limit=10", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	
	timestamps = [log["timestamp"] for log in data["logs"]]
	assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.anyio
async def test_get_single_audit_log(client: AsyncClient) -> None:
	token = await login(client, "admin@opshelf.com", "1234")
	headers = {"Authorization": f"Bearer {token}"}

	# Generate a log entry
	await client.get("/api/users", headers=headers)

	# Get the latest log id
	async with AsyncSessionLocal() as db:
		res = await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
		latest_log = res.scalar_one()
		log_id = latest_log.id

	# Get single log by id
	resp = await client.get(f"/api/audit/logs/{log_id}", headers=headers)
	assert resp.status_code == 200
	data = resp.json()
	assert data["id"] == log_id
	assert data["method"] == "GET"
	assert "/api/users" in data["endpoint"]
	assert "user_id" in data
	assert "timestamp" in data
	assert "response_status" in data

	# Test non-existent log
	resp = await client.get("/api/audit/logs/999999", headers=headers)
	assert resp.status_code == 404


@pytest.mark.anyio
async def test_audit_list_requires_permission(client: AsyncClient) -> None:
	# Create user without audit:read permission
	async with AsyncSessionLocal() as db:
		res = await db.execute(select(Role).filter(Role.name == "viewer"))
		viewer_role = res.scalar_one()
		
		from openshelf_os_server.core.auth import AuthService
		from openshelf_os_server.settings import get_settings
		
		settings = get_settings()
		auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
		
		limited_user = User(
			email="limited@opshelf.com",
			first_name="Limited",
			last_name="User",
			pin_hash=auth.hash_pin("7777"),
			role_id=viewer_role.id,
			is_active=True,
		)
		db.add(limited_user)
		await db.commit()

	token = await login(client, "limited@opshelf.com", "7777")
	headers = {"Authorization": f"Bearer {token}"}

	# Should be forbidden without audit:read permission
	resp = await client.get("/api/audit/logs", headers=headers)
	assert resp.status_code == 403

	resp = await client.get("/api/audit/logs/1", headers=headers)
	assert resp.status_code == 403

	resp = await client.get("/api/audit/verify", headers=headers)
	assert resp.status_code == 403