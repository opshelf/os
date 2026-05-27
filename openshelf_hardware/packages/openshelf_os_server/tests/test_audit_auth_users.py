import pytest
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
async def test_auth_flow_and_audit_chain(client: AsyncClient) -> None:
    token = await login(client, "admin@opshelf.com", "1234")
    headers = {"Authorization": f"Bearer {token}"}

    # Access protected endpoint and current user
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == "admin@opshelf.com"

    # Trigger some activity to create audit logs
    users_list = await client.get("/api/users", headers=headers)
    assert users_list.status_code == 200

    # Verify tamper chain via API
    verify = await client.get("/api/audit/verify", headers=headers)
    assert verify.status_code == 200
    v = verify.json()
    assert v["ok"] is True
    assert v["checked"] >= 1

    # Also spot check DB that latest log has tamper fields
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(1))
        last = res.scalar_one()
        assert last.seq_no is not None
        assert last.entry_hash is not None
        assert last.hmac is not None


@pytest.mark.anyio
async def test_access_control_enforcement(client: AsyncClient) -> None:
    # Create a low-privilege viewer user
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Role).filter(Role.name == "viewer"))
        viewer_role = res.scalar_one()
        # Ensure user exists with limited permissions
        res = await db.execute(select(User).filter(User.email == "viewer@opshelf.com"))
        viewer = res.scalar_one_or_none()
        if viewer is None:
            from openshelf_os_server.core.auth import AuthService
            from openshelf_os_server.settings import get_settings

            settings = get_settings()
            auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
            viewer_user = User(
                email="viewer@opshelf.com",
                first_name="View",
                last_name="Only",
                pin_hash=auth.hash_pin("0000"),
                role_id=viewer_role.id,
                is_active=True,
            )
            db.add(viewer_user)
            await db.commit()

    vtoken = await login(client, "viewer@opshelf.com", "0000")
    vheaders = {"Authorization": f"Bearer {vtoken}"}

    # Attempt to access users listing which requires users:read -> should be 403
    r = await client.get("/api/users", headers=vheaders)
    assert r.status_code == 403

    # Audit verify should also be forbidden for viewer (needs audit:read)
    r2 = await client.get("/api/audit/logs", headers=vheaders)
    assert r2.status_code == 403


@pytest.mark.anyio
async def test_users_crud_as_admin(client: AsyncClient) -> None:
    token = await login(client, "admin@opshelf.com", "1234")
    headers = {"Authorization": f"Bearer {token}"}

    # Create a role for testing
    import uuid
    role_name = f"tester-{uuid.uuid4().hex[:6]}"
    role_resp = await client.post(
        "/api/roles",
        headers=headers,
        json={"name": role_name, "permissions": ["users:read"]},
    )
    assert role_resp.status_code == 200, role_resp.text
    role_id = role_resp.json()["id"]

    # Create user
    create = await client.post(
        "/api/users",
        headers=headers,
        json={
            "email": "testuser@opshelf.com",
            "first_name": "Test",
            "last_name": "User",
            "pin": "5555",
            "role_id": role_id,
        },
    )
    assert create.status_code == 200, create.text
    user_id = create.json()["id"]

    # Get user
    read = await client.get(f"/api/users/{user_id}", headers=headers)
    assert read.status_code == 200
    assert read.json()["email"] == "testuser@opshelf.com"

    # Update user
    upd = await client.put(
        f"/api/users/{user_id}",
        headers=headers,
        json={"first_name": "Tester"},
    )
    assert upd.status_code == 200
    assert upd.json()["first_name"] == "Tester"

    # Deactivate user
    delete = await client.delete(f"/api/users/{user_id}", headers=headers)
    assert delete.status_code == 200

    # List users
    lst = await client.get("/api/users", headers=headers)
    assert lst.status_code == 200


@pytest.mark.anyio
async def test_verify_detects_tamper(client: AsyncClient) -> None:
    token = await login(client, "admin@opshelf.com", "1234")
    headers = {"Authorization": f"Bearer {token}"}

    # Create an entry by hitting an endpoint
    _ = await client.get("/api/users", headers=headers)

    # Tamper with the latest log
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(AuditLog).order_by(AuditLog.seq_no.desc()).limit(1))
        last = res.scalar_one()
        last.response_status = (last.response_status or 200) + 1  # corrupt
        await db.commit()

    verify = await client.get("/api/audit/verify", headers=headers)
    assert verify.status_code == 200
    v = verify.json()
    assert v["ok"] is False
    assert v["first_error_seq"] is not None
