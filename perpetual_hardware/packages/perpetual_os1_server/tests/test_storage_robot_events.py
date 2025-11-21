import pytest
from sqlalchemy import select

from perpetual_os1_server.core.robot_events import process_robot_event
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.database.models import Item, ItemLocation
from perpetual_os1_server.database.session import AsyncSessionLocal


async def _seed_item_with_location(upca: str = "123456", shelf_idx: int = 0, cell_idx: int = 0) -> None:
    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            upca=upca,
            name="Test Item",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=None,
            metadata={"name": "Test Item"},
        )
        await storage.add_item_location(upca, shelf_idx, cell_idx)


@pytest.mark.anyio
async def test_process_robot_event_removes_location_on_success() -> None:
    await _seed_item_with_location(upca="111", shelf_idx=1, cell_idx=2)

    payload = {
        "command": "retrieve_item",
        "message": "success",
        "location": {"side": "left_module", "shelf_idx": 1, "cell_idx": 2},
        "upca": "111",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == "111")
        )
        assert result.scalar_one_or_none() is None


@pytest.mark.anyio
async def test_process_robot_event_ignores_incomplete_payload() -> None:
    await _seed_item_with_location(upca="222", shelf_idx=0, cell_idx=0)

    payload = {
        "command": "retrieve_item",
        "message": "error",
        "location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
        "upca": "222",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == "222")
        )
        assert result.scalar_one_or_none() is not None
