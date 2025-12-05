import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from perpetual_os1_server.main import app
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.core.storage import StorageService


@pytest.mark.anyio
async def test_storage_service_ignores_json_migration() -> None:
    """Legacy JSON migration hooks are disabled."""
    async with AsyncSessionLocal() as session:
        storage_service = StorageService(session)
        assert await storage_service.detect_migration_needed() is False
        assert await storage_service.migrate_from_json() is True


@pytest.mark.anyio
async def test_size_endpoint_returns_database_items(seeded_db: None) -> None:
    """Ensure /api/sizes responds with DB-backed data only."""
    async with AsyncSessionLocal() as session:
        storage_service = StorageService(session)
        await storage_service.create_item(
            upca="size_api_item",
            name="Size API Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0},
            weight=100.0,
            metadata={"tag": "api"},
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/sizes/")
        assert response.status_code == 200
        data = response.json()
        items = data.get("items") or {}
        assert "size_api_item" in items
        assert items["size_api_item"]["data"]["name"] == "Size API Item"


@pytest.mark.anyio
async def test_size_endpoint_creates_via_database(seeded_db: None) -> None:
    """Creating via /api/sizes/ writes to the database and reflects in legacy payload."""
    payload = {
        "upca": "api_created_item",
        "name": "Created via API",
        "dimensions": {"width": 5.0, "height": 6.0, "length": 7.0},
        "weight": 42.0,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/sizes/", json=payload)
        assert response.status_code == 200
        data = response.json()
        items = data.get("items") or {}
        assert "api_created_item" in items

    async with AsyncSessionLocal() as session:
        storage_service = StorageService(session)
        db_item = await storage_service.get_item("api_created_item")
        assert db_item is not None
        assert db_item.name == "Created via API"
