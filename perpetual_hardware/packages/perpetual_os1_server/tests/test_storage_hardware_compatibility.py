import pytest

from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.core.storage import StorageService


@pytest.mark.anyio
async def test_storage_service_ignores_json_migration() -> None:
    """Legacy JSON migration hooks are disabled."""
    async with AsyncSessionLocal() as session:
        storage_service = StorageService(session)
        assert await storage_service.detect_migration_needed() is False
        assert await storage_service.migrate_from_json() is True


