import os
import asyncio
import tempfile
from pathlib import Path
import pytest
import pytest_asyncio
from typing import AsyncGenerator

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Ensure env is set BEFORE importing any app modules that use the database.
# The repo-level conftest usually does this first (test modules from other
# packages can import the app before this conftest runs); the guard keeps a
# single temp dir when both run.
if os.environ.get("OPENSHELF_TEST_ENV_INITIALIZED") != "1":
	_tmpdir = Path(tempfile.mkdtemp(prefix="perp_os1_testdb_"))
	_db_path = _tmpdir / "test.db"
	os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
	os.environ["JWT_SECRET"] = "test-jwt-secret"
	os.environ["AUDIT_HMAC_KEY"] = "test-hmac-key"
	os.environ["ENV"] = "test"
	os.environ["OPENSHELF_ONBOARDING_STATE_PATH"] = str(_tmpdir / "onboarding_state")
	# Isolate the suite from the repo's data/ runtime state (mode file, config.json).
	os.environ["OPENSHELF_DATA_DIR"] = str(_tmpdir)
	os.environ["OPERATING_MODE"] = "development"
	os.environ["OPENSHELF_TEST_ENV_INITIALIZED"] = "1"

from sqlalchemy import delete

from openshelf_os_server.database.models import (
    AccessLevel,
    AuditLog,
    ImportJob,
    ImportJobUnit,
    Item,
    ItemLocation,
    Role,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
    StorageCell,
    User,
)

import httpx
from openshelf_os_server.main import app
from openshelf_os_server.database.session import AsyncSessionLocal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from openshelf_os_server.core.auth import AuthService
from openshelf_os_server.settings import get_settings


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    # httpx AsyncClient uses asyncio
    return "asyncio"


@pytest_asyncio.fixture(scope="session")
async def seeded_db() -> AsyncGenerator[None, None]:
    # Ensure tables exist, then seed roles and an admin user
    from openshelf_os_server.database.base import engine, Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        # Roles
        roles = {
            "admin": [
                "users:read", "users:write", "users:delete",
                "roles:read", "roles:write", "roles:delete",
                "robot:control",
                "storage:read", "storage:write", "storage:delete", "storage:access",
                "audit:read"
            ],
            "operator": [
                "users:read",
                "storage:read",
            ],
            "viewer": [
                "storage:read"
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

    async def _clear(session: AsyncSession) -> None:
        if is_storage:
            await session.execute(delete(ImportJobUnit))
            await session.execute(delete(ImportJob))
            await session.execute(delete(StorageBinWorkflow))
            await session.execute(delete(StorageBinItem))
            await session.execute(delete(StorageBin))
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
    yield
    async with AsyncSessionLocal() as session:
        await _clear(session)


@pytest.fixture
def auth_headers(seeded_db: AsyncGenerator[None, None]) -> dict[str, str]:
    """Create auth headers with a valid admin token."""
    settings = get_settings()
    auth = AuthService(settings.auth.jwt_secret, settings.auth.jwt_algorithm)
    token = auth.create_access_token(
        user_id=1,
        email="admin@opshelf.com",
        role="admin",
        permissions=["users:read", "users:write", "robot:control", "storage:read", "storage:write", "storage:access"],
        access_level=None,
        expires_minutes=60,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client(seeded_db: AsyncGenerator[None, None], auth_headers: dict[str, str]) -> AsyncGenerator[httpx.AsyncClient, None]:
    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True, headers=auth_headers) as ac:
        yield ac


@pytest_asyncio.fixture
async def unauthenticated_client(seeded_db: AsyncGenerator[None, None]) -> AsyncGenerator[httpx.AsyncClient, None]:
    """Client without authentication headers for testing unauthenticated requests."""
    from httpx import AsyncClient, ASGITransport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=True) as ac:
        yield ac
