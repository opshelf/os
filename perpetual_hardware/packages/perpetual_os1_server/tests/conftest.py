import os
import asyncio
import tempfile
from pathlib import Path
import pytest
import pytest_asyncio
from typing import AsyncGenerator

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Ensure env is set BEFORE importing any app modules that use the database
_tmpdir = Path(tempfile.mkdtemp(prefix="perp_os1_testdb_"))
_db_path = _tmpdir / "test.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
os.environ["JWT_SECRET"] = "test-jwt-secret"
os.environ["AUDIT_HMAC_KEY"] = "test-hmac-key"
os.environ["ENV"] = "test"

from sqlalchemy import delete

from perpetual_os1_server.database.models import AccessLevel, AuditLog, Item, ItemLocation, Role, StorageCell, User
from perpetual_os1_server.core import sizes as size_utils

import httpx
from perpetual_os1_server.main import app
from perpetual_os1_server.database.session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from perpetual_os1_server.core.auth import AuthService
from perpetual_os1_server.settings import get_settings


@pytest.fixture(scope="session")
def anyio_backend() -> str:
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
                "robot:control", "robot:read",
                "gantry:control", "gantry:read",
                "tool:control", "tool:read",
                "storage:read", "storage:write", "storage:delete",
                "scanner:read", "scanner:control",
                "workstation:control", "workstation:read",
                "audit:read"
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
        user_res = await db.execute(select(User).filter(User.email == "admin@opshelf.com"))
        admin_user: User | None = user_res.scalar_one_or_none()
        if admin_user is None:
            admin_user = User(
                email="admin@opshelf.com",
                first_name="Admin",
                last_name="User",
                pin_hash=auth.hash_pin("1234"),
                role_id=admin_role.id,
                is_active=True,
            )
            db.add(admin_user)
            await db.commit()
        yield


@pytest_asyncio.fixture(autouse=True)
async def reset_storage_tables(
    seeded_db: AsyncGenerator[None, None],
    request: pytest.FixtureRequest
) -> AsyncGenerator[None, None]:
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

    async def _clear(session: AsyncSession) -> None:
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
async def client(seeded_db: AsyncGenerator[None, None]) -> AsyncGenerator[httpx.AsyncClient, None]:
    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as ac:
        yield ac
