import pytest
from sqlalchemy import select
from datetime import datetime

from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.database.models import Item, ItemLocation, StorageCell
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.validation import ValidationError


@pytest.mark.anyio
async def test_item_creation() -> None:
    """Test creating an item in the database."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        item = await service.create_item(
            barcode="123456789012",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0},
            weight=500.0,
            metadata={"category": "test", "priority": 1}
        )

        assert item.id is not None
        assert item.barcode == "123456789012"
        assert item.name == "Test Item"
        assert item.dimensions == {"width": 10.0, "height": 20.0, "length": 30.0}
        assert item.weight == 500
        assert item.item_metadata == {"category": "test", "priority": 1}
        assert item.created_at is not None
        assert item.updated_at is not None


@pytest.mark.anyio
async def test_item_get() -> None:
    """Test retrieving an item by barcode."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Create item first
        created_item = await service.create_item(
            barcode="123456789012",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

        # Get item
        item = await service.get_item("123456789012")

        assert item is not None
        assert item.id == created_item.id
        assert item.barcode == "123456789012"
        assert item.name == "Test Item"


@pytest.mark.anyio
async def test_item_get_nonexistent() -> None:
    """Test retrieving a non-existent item."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        item = await service.get_item("nonexistent")

        assert item is None


@pytest.mark.anyio
async def test_item_update() -> None:
    """Test updating an item."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Create item first
        created_item = await service.create_item(
            barcode="123456789012",
            name="Original Name",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

        # Update item
        updated_item = await service.update_item(
            barcode="123456789012",
            name="Updated Name",
            weight=1000.0
        )

        assert updated_item is not None
        assert updated_item.id == created_item.id
        assert updated_item.name == "Updated Name"
        assert updated_item.weight == 1000
        assert updated_item.dimensions == {"width": 10.0, "height": 20.0, "length": 30.0}


@pytest.mark.anyio
async def test_item_update_nonexistent() -> None:
    """Test updating a non-existent item."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        updated_item = await service.update_item(
            barcode="nonexistent",
            name="Updated Name"
        )

        assert updated_item is None


@pytest.mark.anyio
async def test_item_delete() -> None:
    """Test deleting an item."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Create item first
        created_item = await service.create_item(
            barcode="123456789012",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

        # Delete item
        success = await service.delete_item("123456789012")

        assert success is True

        # Verify item is deleted
        item = await service.get_item("123456789012")
        assert item is None


@pytest.mark.anyio
async def test_item_delete_nonexistent() -> None:
    """Test deleting a non-existent item."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        success = await service.delete_item("nonexistent")

        assert success is False


@pytest.mark.anyio
async def test_get_all_items() -> None:
    """Test retrieving all items with pagination."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Create multiple items
        await service.create_item("item1", "Item 1", {"width": 10, "height": 10, "length": 10})
        await service.create_item("item2", "Item 2", {"width": 20, "height": 20, "length": 20})
        await service.create_item("item3", "Item 3", {"width": 30, "height": 30, "length": 30})

        # Get all items
        items = await service.get_all_items()

        assert len(items) == 3

        # Test pagination
        items_page1 = await service.get_all_items(skip=0, limit=2)
        assert len(items_page1) == 2

        items_page2 = await service.get_all_items(skip=2, limit=2)
        assert len(items_page2) == 1


@pytest.mark.anyio
async def test_item_location() -> None:
    """Test adding and retrieving item locations."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Create item first
        item = await service.create_item(
            barcode="123456789012",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

        # Add location
        location = await service.add_item_location(
            barcode="123456789012",
            side="front_module",
            shelf_idx=1,
            cell_idx=2
        )

        assert location is not None
        assert location.item_id == item.id
        assert location.shelf_idx == 1
        assert location.cell_idx == 2

        # Get locations
        locations = await service.get_item_locations("123456789012")

        assert len(locations) == 1
        assert locations[0].shelf_idx == 1
        assert locations[0].cell_idx == 2


@pytest.mark.anyio
async def test_item_location_remove() -> None:
    """Test removing item locations."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Create item and add location
        item = await service.create_item(
            barcode="123456789012",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

        await service.add_item_location(barcode="123456789012", side="front_module", shelf_idx=1, cell_idx=2)

        # Remove location
        success = await service.remove_item_location(
            barcode="123456789012",
            side="front_module",
            shelf_idx=1,
            cell_idx=2
        )

        assert success is True

        # Verify location is removed
        locations = await service.get_item_locations("123456789012")
        assert len(locations) == 0


@pytest.mark.anyio
async def test_storage_cell_operations() -> None:
    """Test storage cell get/update operations."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        # Seed a cell directly: creation is not part of the service API.
        cell = StorageCell(shelf_idx=1, cell_idx=1, capacity="small")
        db.add(cell)
        await db.commit()
        await db.refresh(cell)

        assert cell.id is not None
        assert cell.status == "available"

        # Get storage cells
        cells = await service.get_storage_cells()

        assert len(cells) == 1
        assert cells[0].shelf_idx == 1
        assert cells[0].cell_idx == 1

        # Update cell status
        success = await service.update_storage_cell(
            shelf_idx=1,
            cell_idx=1,
            status="occupied"
        )

        assert success is True

        # Verify update
        cells = await service.get_storage_cells(shelf_idx=1, cell_idx=1)
        assert cells[0].status == "occupied"


@pytest.mark.anyio
async def test_get_all_occupied_cells() -> None:
    """Test retrieving all occupied cells from the database."""
    async with AsyncSessionLocal() as db:
        service = StorageService(db)

        result = await service.get_all_occupied_cells()
        assert result == []

        await service.create_item(
            barcode="item1",
            name="Item 1",
            dimensions={"width": 10, "height": 10, "length": 10}
        )
        await service.create_item(
            barcode="item2",
            name="Item 2",
            dimensions={"width": 20, "height": 20, "length": 20}
        )

        await service.add_item_location(barcode="item1", side="front_module", shelf_idx=0, cell_idx=1)
        await service.add_item_location(barcode="item1", side="back_module", shelf_idx=2, cell_idx=3)
        await service.add_item_location(barcode="item2", side="front_module", shelf_idx=1, cell_idx=0)

        result = await service.get_all_occupied_cells()

        assert len(result) == 3

        occupied_set = {(row[0], row[1], row[2], row[3]) for row in result}
        assert ("front_module", 0, 1, "item1") in occupied_set
        assert ("back_module", 2, 3, "item1") in occupied_set
        assert ("front_module", 1, 0, "item2") in occupied_set
