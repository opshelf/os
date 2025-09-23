import os
import asyncio
import tempfile
from pathlib import Path
import pytest
from typing import AsyncGenerator

from sqlalchemy import delete

from perpetual_os1_server.database.models import AuditLog, Item, ItemLocation, Role, StorageCell, User
from perpetual_os1_server.core import sizes as size_utils

# Ensure env is set before importing the app/DB engine
_tmpdir = Path(tempfile.mkdtemp(prefix="perp_os1_testdb_"))
_db_path = _tmpdir / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["AUDIT_HMAC_KEY"] = "test-hmac-key"


import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
from perpetual_os1_server.main import app
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.database.models import Role, User
from sqlalchemy import select
from perpetual_os1_server.core.auth import AuthService
from perpetual_os1_server.settings import get_settings


@pytest.fixture(scope="session")
def anyio_backend():
    # httpx AsyncClient uses asyncio
    return "asyncio"


@pytest.fixture(scope="session")
async def seeded_db() -> AsyncGenerator[None, None]:
    # Ensure tables exist, then seed roles and an admin user
    from perpetual_os1_server.database.base import engine, Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Roles
        roles = {
            "admin": [
                "users:read", "users:write", "users:delete",
                "roles:read", "roles:write", "roles:delete",
                "audit:read",
                "storage:read", "storage:write", "storage:delete",
            ],
            "operator": [
                "users:read",
                "workstation:read", "workstation:write",
                "robot:read", "robot:write",
            ],
            "viewer": [
                "workstation:read"
            ],
        }
        for name, perms in roles.items():
            res = await db.execute(select(Role).filter(Role.name == name))
            role = res.scalar_one_or_none()
            if role is None:
                role = Role(name=name, permissions=perms)
                db.add(role)
        await db.commit()

        # Admin user
        settings = get_settings()
        auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
        res = await db.execute(select(Role).filter(Role.name == "admin"))
        admin_role = res.scalar_one()
        res = await db.execute(select(User).filter(User.email == "admin@opshelf.com"))
        admin = res.scalar_one_or_none()
        if admin is None:
            admin = User(
                email="admin@opshelf.com",
                first_name="Admin",
                last_name="User",
                pin_hash=auth.hash_pin("1234"),
                role_id=admin_role.id,
                is_active=True,
            )
            db.add(admin)
            await db.commit()
        yield


@pytest.fixture(autouse=True)
async def reset_storage_tables(seeded_db, request):
    """Ensure test fixtures leave storage and audit data in a clean state."""
    module_name = getattr(getattr(request.node, "module", None), "__name__", "")
    is_storage = "test_storage" in module_name
    is_audit = "test_audit" in module_name

    if not (is_storage or is_audit):
        yield
        return

    def _reset_size_db() -> None:
        size_utils.size_database = None
        size_utils._size_db_path = None
        size_path = size_utils.get_sizes_filename()
        if os.path.exists(size_path):
            try:
                os.remove(size_path)
            except OSError:
                pass

    async def _clear(session):
        if is_storage:
            await session.execute(delete(ItemLocation))
            await session.execute(delete(Item))
            await session.execute(delete(StorageCell))
        if is_audit:
            await session.execute(delete(AuditLog))
            await session.execute(delete(User).where(User.email != "admin@opshelf.com"))
            await session.execute(
                delete(Role).where(~Role.name.in_(["admin", "operator", "viewer"]))
            )
        await session.commit()

    async with AsyncSessionLocal() as session:
        await _clear(session)
    if is_storage:
        _reset_size_db()
    yield
    async with AsyncSessionLocal() as session:
        await _clear(session)
    if is_storage:
        _reset_size_db()


@pytest.fixture
async def client(seeded_db):
    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as ac:
        yield ac
