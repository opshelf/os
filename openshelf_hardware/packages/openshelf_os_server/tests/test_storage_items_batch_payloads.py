"""Paged inventory reads must not re-read a tray once per member item."""

from typing import Iterator, List, Tuple

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from openshelf_os_server.core.bin_storage import BinStorageService
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.database.base import engine
from openshelf_os_server.database.models import Item, StorageBin, StorageBinItem
from openshelf_os_server.database.session import AsyncSessionLocal

_members = 0


@pytest.fixture
def statements() -> Iterator[List[str]]:
    """Record every SQL statement the shared engine executes for this test."""
    recorded: List[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        recorded.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield recorded
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _tray(cell_idx: int, status: str = "at_home", gross_weight: float | None = None) -> StorageBin:
    return StorageBin(
        barcode=f"90000000{cell_idx:04d}",
        side="front_module",
        shelf_idx=0,
        cell_idx=cell_idx,
        inner_dimensions={"width": 200.0, "height": 100.0, "length": 160.0},
        max_items=4,
        status=status,
        gross_weight=gross_weight,
    )


async def _seed(
    db: AsyncSession,
    counts: List[int],
    cell_start: int = 0,
    status: str = "at_home",
    gross_weight: float | None = None,
) -> List[Tuple[Item, int]]:
    """Create one tray per count, each filled with that many tray members."""
    global _members
    storage = StorageService(db)
    rows: List[Tuple[Item, int]] = []
    for offset, count in enumerate(counts):
        tray = _tray(cell_start + offset, status, gross_weight)
        db.add(tray)
        await db.flush()
        for compartment in range(1, count + 1):
            _members += 1
            item = await storage.create_item(
                f"123456789{_members:03d}",
                f"Bottle {_members}",
                {"width": 40.0, "height": 90.0, "length": 70.0},
            )
            db.add(StorageBinItem(bin_id=tray.id, item_id=item.id, compartment=compartment))
            rows.append((item, int(tray.id)))
    await db.commit()
    return rows


@pytest.mark.anyio
async def test_batched_bin_payloads_match_the_per_item_payload(statements: List[str]) -> None:
    async with AsyncSessionLocal() as db:
        rows = await _seed(db, [3, 2])
    async with AsyncSessionLocal() as db:
        service = BinStorageService(db)
        batched = await service.item_bin_payloads([item.id for item, _ in rows])
        assert len(batched) == 5
        for item, _ in rows:
            assert batched[item.id] == await service.item_bin_payload(item.id)
        # Items that share a tray report that tray's whole contents, in name order.
        by_barcode = [entry["barcode"] for entry in batched[rows[0][0].id]["items"]]
        assert by_barcode == sorted(by_barcode)
        assert batched[rows[0][0].id]["item_count"] == 3
        assert batched[rows[3][0].id]["item_count"] == 2
    async with AsyncSessionLocal() as db:
        # An item without a tray has no payload, and an empty request costs no query.
        loose = await StorageService(db).create_item("777777777777", "Loose", {"width": 1, "height": 1, "length": 1})
        assert await BinStorageService(db).item_bin_payloads([loose.id]) == {}
        assert await BinStorageService(db).item_bin_payloads([]) == {}


@pytest.mark.anyio
async def test_items_endpoint_queries_do_not_grow_with_tray_members(
    client: AsyncClient, statements: List[str]
) -> None:
    async with AsyncSessionLocal() as db:
        await _seed(db, [1])
    statements.clear()
    first = await client.get("/api/storage/items", params={"limit": 50})
    first_queries = len(statements)
    assert first.status_code == 200
    assert first.json()["total"] == 1
    assert first.headers["x-total-count"] == "1"

    async with AsyncSessionLocal() as db:
        await _seed(db, [4, 3], cell_start=1)
        # A tray that is outside home and an item in no tray must serialize through the same
        # batch path: status gates gross_weight and can_retrieve.
        await _seed(db, [2], cell_start=3, status="outside", gross_weight=1500.0)
        await StorageService(db).create_item(
            "777777777777", "Loose", {"width": 1.0, "height": 1.0, "length": 1.0}
        )
    statements.clear()
    second = await client.get("/api/storage/items", params={"limit": 50})
    second_queries = len(statements)
    assert second.status_code == 200

    assert first_queries == second_queries

    payload = second.json()
    assert payload["total"] == 11
    assert second.headers["x-total-count"] == "11"
    assert second.headers["x-limit"] == "50"
    assert second.headers["x-skip"] == "0"
    binned = [entry for entry in payload["items"] if entry["bin"] is not None]
    assert len(binned) == 10
    assert [entry["barcode"] for entry in payload["items"] if entry["bin"] is None] == [
        "777777777777"
    ]
    assert sorted(entry["bin"]["item_count"] for entry in binned) == [
        1,
        2,
        2,
        3,
        3,
        3,
        4,
        4,
        4,
        4,
    ]
    for entry in binned:
        home = entry["bin"]["home_location"]
        derived = [
            location
            for location in entry["locations"]
            if (location["side"], location["shelf_idx"], location["cell_idx"]) == (
                home["side"],
                home["shelf_idx"],
                home["cell_idx"],
            )
        ]
        assert [location["id"] for location in derived] == [-entry["bin"]["id"]]
    at_home = [entry for entry in binned if entry["bin"]["status"] == "at_home"]
    outside = [entry for entry in binned if entry["bin"]["status"] == "outside"]
    assert all(entry["can_retrieve"] is True for entry in at_home)
    assert len(at_home) == 8
    assert {entry["bin"]["barcode"] for entry in at_home} == {
        "900000000000",
        "900000000001",
        "900000000002",
    }
    # A tray away from its home cell is readable but not directly retrievable, and its recorded
    # mass stays hidden until it is home again.
    assert len(outside) == 2
    assert {entry["bin"]["barcode"] for entry in outside} == {"900000000003"}
    assert all(entry["can_retrieve"] is False for entry in outside)
    assert all(entry["bin"]["gross_weight"] is None for entry in outside)

