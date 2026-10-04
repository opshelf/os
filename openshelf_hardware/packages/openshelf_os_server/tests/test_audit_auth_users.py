import pytest
from sqlalchemy import select
from httpx import AsyncClient

from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.database.models import AuditLog


async def login(client: AsyncClient, email: str, pin: str) -> str:
    resp = await client.post("/api/auth/login/json", json={"email": email, "pin": pin})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return str(data["access_token"])


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
