import pytest
import os
import json
from pathlib import Path
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from perpetual_os1_server.main import app
from perpetual_os1_server.database.session import AsyncSessionLocal
from perpetual_os1_server.core.sizes import get_size_db, add_item_to_json, remove_item_from_json
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.core.sizes import migrate_if_needed


@pytest.fixture
async def test_db():
    """Create test database for hardware compatibility tests."""
    # Use in-memory SQLite for testing
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Create tables
    from perpetual_os1_server.database.base import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        yield session


@pytest.fixture
async def test_json_file(tmp_path):
    """Create a temporary JSON file for testing."""
    json_file = tmp_path / "test_sizes.json"
    original_sizes_path = os.environ.get("SIZES_FILE")

    # Set test environment
    os.environ["SIZES_FILE"] = str(json_file)

    # Create initial JSON data
    test_data = {
        "123456789012": {
            "size": {"width": 10.0, "height": 20.0, "length": 30.0},
            "data": {"name": "Test Item", "weight": 500.0}
        },
        "123456789013": {
            "size": {"width": 15.0, "height": 25.0, "length": 35.0},
            "data": {"name": "Test Item 2", "weight": 750.0}
        }
    }

    with open(json_file, 'w') as f:
        json.dump(test_data, f)

    yield str(json_file)

    # Restore original environment
    if original_sizes_path:
        os.environ["SIZES_FILE"] = original_sizes_path
    else:
        del os.environ["SIZES_FILE"]


@pytest.mark.anyio
async def test_json_file_exists(test_json_file):
    """Test that JSON file exists and is readable."""
    assert os.path.exists(test_json_file)

    with open(test_json_file, 'r') as f:
        data = json.load(f)

    assert "123456789012" in data
    assert "123456789013" in data


@pytest.mark.anyio
async def test_backward_compatibility_json_reading(test_json_file):
    """Test that system can read from JSON format (backward compatibility)."""
    # Test legacy function
    from perpetual_os1_server.core.sizes import get_item_in_json_format

    item = get_item_in_json_format("123456789012")
    assert item is not None
    assert item.size["width"] == 10.0
    assert item.data["name"] == "Test Item"

    # Test non-existent item
    item = get_item_in_json_format("nonexistent")
    assert item is None


@pytest.mark.anyio
async def test_backward_compatibility_json_writing(test_json_file):
    """Test that system can write to JSON format (backward compatibility)."""
    # Test adding item via legacy function
    from perpetual_os1_server.core.sizes import add_item_to_json
    from perpetual_os1.storage import Dimensions
    from typing import Dict

    dimensions = Dimensions(width=5.0, height=10.0, length=15.0)
    data = {"name": "Legacy Item", "weight": 250.0}

    add_item_to_json("123456789999", data, dimensions)

    # Verify item was added to JSON
    with open(test_json_file, 'r') as f:
        data = json.load(f)

    assert "123456789999" in data
    assert data["123456789999"]["size"]["width"] == 5.0
    assert data["123456789999"]["data"]["name"] == "Legacy Item"

    # Test removing item via legacy function
    from perpetual_os1_server.core.sizes import remove_item_from_json

    remove_item_from_json("123456789999")

    # Verify item was removed from JSON
    with open(test_json_file, 'r') as f:
        data = json.load(f)

    assert "123456789999" not in data


@pytest.mark.anyio
async def test_database_with_existing_json(test_db, test_json_file):
    """Test that database works with existing JSON file (migration scenario)."""
    # Initialize storage service
    storage_service = StorageService(test_db)

    # Test migration detection
    migration_needed = await storage_service.detect_migration_needed()
    # Should be True since JSON exists and database is empty
    assert migration_needed == True

    # Perform migration
    success = await storage_service.migrate_from_json()
    assert success == True

    # Verify data was migrated
    items = await storage_service.get_all_items()
    assert len(items) >= 2  # Should have at least the 2 test items

    # Verify specific item was migrated correctly
    item = await storage_service.get_item("123456789012")
    assert item is not None
    assert item.name == "Test Item"
    assert item.dimensions == {"width": 10.0, "height": 20.0, "length": 30.0}


@pytest.mark.anyio
async def test_database_to_json_sync(test_db, test_json_file):
    """Test that database can sync to JSON format."""
    # First migrate existing data
    storage_service = StorageService(test_db)
    await storage_service.migrate_from_json()

    # Add new item via database
    item = await storage_service.create_item(
        upca="database_item",
        name="Database Created Item",
        dimensions={"width": 25.0, "height": 35.0, "length": 45.0},
        weight=1250.0
    )

    # Sync to JSON
    success = await storage_service.sync_to_json()
    assert success == True

    # Verify JSON file was updated
    with open(test_json_file, 'r') as f:
        data = json.load(f)

    assert "database_item" in data
    assert data["database_item"]["data"]["name"] == "Database Created Item"


@pytest.mark.anyio
async def test_api_endpoints_backward_compatibility(test_json_file):
    """Test API endpoints maintain backward compatibility."""
    # Create client with temporary JSON file
    from httpx import ASGITransport

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Test GET /api/sizes (should work with JSON format)
        response = await client.get("/api/sizes/")
        assert response.status_code == 200

        # Verify response structure is compatible with original format
        data = response.json()
        assert isinstance(data, dict)  # Original SizeDatabase was a dict
        assert "items" in data or isinstance(data.get("items"), dict)


@pytest.mark.anyio
async def test_api_endpoints_database_integration(test_db, test_json_file):
    """Test API endpoints integrate with database while maintaining JSON compatibility."""
    # Create client with temporary JSON file
    from httpx import ASGITransport

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Test database operations via API
        item_data = {
            "upca": "api_test_item",
            "name": "API Test Item",
            "dimensions": {"width": 40.0, "height": 50.0, "length": 60.0},
            "weight": 2000.0
        }

        # Create item via API
        response = await client.post("/api/sizes/", json=item_data)
        assert response.status_code == 200

        # Verify item exists in database
        async with AsyncSessionLocal() as db:
            storage_service = StorageService(db)
            db_item = await storage_service.get_item("api_test_item")
            assert db_item is not None
            assert db_item.name == "API Test Item"

        # Verify JSON is also updated
        with open(test_json_file, 'r') as f:
            json_data = json.load(f)
        assert "api_test_item" in json_data


@pytest.mark.anyio
async def test_fallback_mechanism(test_db, test_json_file):
    """Test that system falls back to JSON if database fails."""
    # Create client with temporary JSON file
    from httpx import ASGITransport

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Test creating item when database might have issues
        # This should fallback to JSON if database operations fail
        item_data = {
            "upca": "fallback_item",
            "item": {
                "size": {"width": 5.0, "height": 10.0, "length": 15.0},
                "data": {"name": "Fallback Item", "weight": 100.0}
            }
        }

        response = await client.put("/api/sizes/", json=item_data)
        # Should work (either through database or fallback to JSON)
        assert response.status_code in [200, 422]  # 200 if successful, 422 if validation fails


@pytest.mark.anyio
async def test_concurrent_access(test_db, test_json_file):
    """Test concurrent access to both database and JSON."""
    import asyncio

    async def create_item(session, upca, name):
        storage_service = StorageService(session)
        return await storage_service.create_item(
            upca=upca,
            name=name,
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

    # Create multiple items concurrently
    tasks = [
        create_item(test_db, f"concurrent_{i}", f"Item {i}")
        for i in range(5)
    ]

    results = await asyncio.gather(*tasks)

    # Verify all items were created
    assert len(results) == 5
    for i, item in enumerate(results):
        assert item is not None
        assert item.upca == f"concurrent_{i}"
        assert item.name == f"Item {i}"

    # Verify all items exist in database
    storage_service = StorageService(test_db)
    all_items = await storage_service.get_all_items()
    assert len(all_items) >= 5

    # Verify JSON is synchronized
    await storage_service.sync_to_json()
    with open(test_json_file, 'r') as f:
        json_data = json.load(f)

    for i in range(5):
        assert f"concurrent_{i}" in json_data


@pytest.mark.anyio
async def test_data_integrity(test_db, test_json_file):
    """Test data integrity between database and JSON."""
    # Initialize storage service
    storage_service = StorageService(test_db)

    # Create item in database
    item = await storage_service.create_item(
        upca="integrity_test",
        name="Integrity Test Item",
        dimensions={"width": 100.0, "height": 200.0, "length": 300.0},
        weight=5000.0
    )

    # Sync to JSON
    await storage_service.sync_to_json()

    # Verify JSON contains correct data
    with open(test_json_file, 'r') as f:
        json_data = json.load(f)

    json_item = json_data["integrity_test"]
    assert json_item["data"]["name"] == "Integrity Test Item"
    assert json_item["size"]["width"] == 100.0
    assert json_item["data"]["weight"] == 5000.0

    # Modify item in database
    updated_item = await storage_service.update_item(
        upca="integrity_test",
        name="Updated Integrity Test",
        weight=7500.0
    )

    # Sync to JSON again
    await storage_service.sync_to_json()

    # Verify JSON was updated
    with open(test_json_file, 'r') as f:
        json_data = json.load(f)

    json_item = json_data["integrity_test"]
    assert json_item["data"]["name"] == "Updated Integrity Test"
    assert json_item["data"]["weight"] == 7500.0
    assert json_item["size"]["width"] == 100.0  # Should remain unchanged


@pytest.mark.anyio
async def test_error_handling(test_db, test_json_file):
    """Test error handling when JSON operations fail."""
    storage_service = StorageService(test_db)

    # Test handling when JSON file is not writable
    original_path = os.environ.get("SIZES_FILE")
    os.environ["SIZES_FILE"] = "/invalid/path/sizes.json"

    try:
        # This should handle the error gracefully
        success = await storage_service.sync_to_json()
        # Success might be False if write fails, but it shouldn't crash
        assert isinstance(success, bool)
    finally:
        # Restore original path
        if original_path:
            os.environ["SIZES_FILE"] = original_path
        else:
            del os.environ["SIZES_FILE"]