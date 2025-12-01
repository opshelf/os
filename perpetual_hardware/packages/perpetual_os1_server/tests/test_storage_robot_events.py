import pytest
from sqlalchemy import select

from perpetual_os1_server.core.robot_events import process_robot_event
from perpetual_os1_server.core.storage import StorageService
from perpetual_os1_server.database.models import Item, ItemLocation
from perpetual_os1_server.database.session import AsyncSessionLocal


async def _seed_item_with_location(upca: str, side: str, shelf_idx: int, cell_idx: int) -> None:
    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            upca=upca,
            name="Test Item",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=None,
            metadata={"name": "Test Item"},
        )
        await storage.add_item_location(upca, side, shelf_idx, cell_idx)


@pytest.mark.anyio
async def test_process_robot_event_removes_location_on_success() -> None:
    await _seed_item_with_location(upca="111", side="left_module", shelf_idx=1, cell_idx=2)

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
    await _seed_item_with_location(upca="222", side="left_module", shelf_idx=0, cell_idx=0)

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


@pytest.mark.anyio
async def test_process_robot_event_adds_location_on_store_success() -> None:
    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            upca="333",
            name="Store Target",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=None,
            metadata={"name": "Store Target"},
        )

    payload = {
        "command": "store_item",
        "message": "success",
        "location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 4},
        "upca": "333",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == "333")
        )
        location = result.scalar_one_or_none()
        assert location is not None
        assert location.shelf_idx == 2
        assert location.cell_idx == 4


@pytest.mark.anyio
async def test_process_robot_event_creates_item_when_missing() -> None:
    payload = {
        "command": "store_item",
        "message": "success",
        "location": {"side": "left_module", "shelf_idx": 1, "cell_idx": 1},
        "upca": "444",
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        item_result = await session.execute(select(Item).where(Item.upca == "444"))
        item = item_result.scalar_one_or_none()
        assert item is not None

        location_result = await session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == "444")
        )
        location = location_result.scalar_one_or_none()
        assert location is not None


@pytest.mark.anyio
async def test_process_robot_event_updates_weight_from_scale() -> None:
    upca = "555"

    async with AsyncSessionLocal() as session:
        storage = StorageService(session)
        await storage.create_item(
            upca=upca,
            name="Has Weight",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            weight=50.0,
            metadata={"name": "Has Weight"},
            sync_to_json=False,
        )

    payload = {
        "command": "store_item",
        "message": "success",
        "location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
        "upca": upca,
        "weight": 152.0,
    }

    await process_robot_event(payload)

    async with AsyncSessionLocal() as session:
        item_result = await session.execute(select(Item).where(Item.upca == upca))
        item = item_result.scalar_one_or_none()

        assert item is not None
        assert item.weight == 152.0
