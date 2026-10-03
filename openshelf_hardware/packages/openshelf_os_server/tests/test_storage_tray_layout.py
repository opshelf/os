"""Tray compartment-layout editing endpoint (PUT /api/storage/bins/tray-layout)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from sqlalchemy import select

from openshelf_os.config.settings import (
    CompartmentLayoutSettings,
    StorageBinDimensionsSettings,
    StorageBinLocationSettings,
    StorageBinSettings,
)
from openshelf_os.storage import Cell, CellStatus, Dimensions, Shelf, StorageGrid
from openshelf_os_server.core.bin_storage import BinStorageService
import openshelf_os_server.core.bin_storage as bin_storage_core
from openshelf_os_server.database.models import Item, StorageBin, StorageBinItem
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import storage as storage_router
from openshelf_os_server.settings import get_root_config

BARCODE = "123456789012"


def _bin_entry(rows: int | None, columns: int | None) -> StorageBinSettings:
    layout = (
        CompartmentLayoutSettings(rows=rows, columns=columns)
        if rows is not None and columns is not None
        else None
    )
    return StorageBinSettings(
        barcode=BARCODE,
        home_location=StorageBinLocationSettings(side="front_module", shelf_idx=0, cell_idx=1),
        inner_dimensions=StorageBinDimensionsSettings(width=200.0, height=50.0, length=300.0),
        max_items=(rows or 1) * (columns or 1),
        compartment_layout=layout,
    )


def _config_with_bins(monkeypatch: pytest.MonkeyPatch, entries: list[StorageBinSettings]):
    base = get_root_config().model_copy(deep=True)
    base.storage.bins = entries
    monkeypatch.setattr(storage_router, "get_root_config", lambda: base)
    monkeypatch.setattr(storage_router, "clear_config_cache", lambda: None)
    monkeypatch.setattr(storage_router.BinStorageService, "sync_config", AsyncMock())
    return base


@pytest.mark.anyio
async def test_tray_layout_updates_config(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    saved = {}

    def _record(config):
        saved["config"] = config

    monkeypatch.setattr(storage_router, "save_root_config", _record)

    response = await client.put(
        "/api/storage/bins/tray-layout",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        json={"rows": 4, "columns": 5},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True}
    entry = saved["config"].storage.bins[0]
    assert entry.compartment_layout is not None
    assert (entry.compartment_layout.rows, entry.compartment_layout.columns) == (4, 5)
    assert entry.max_items == 20


@pytest.mark.anyio
async def test_tray_layout_missing_cell(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    response = await client.put(
        "/api/storage/bins/tray-layout",
        params={"side": "front_module", "shelf_idx": 7, "cell_idx": 0},
        json={"rows": 2, "columns": 2},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_tray_layout_refuses_dirty_tray(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    async with AsyncSessionLocal() as session:
        session.add(
            StorageBin(
                barcode=BARCODE,
                side="front_module",
                shelf_idx=0,
                cell_idx=1,
                inner_dimensions={"width": 200.0, "height": 50.0, "length": 300.0},
                max_items=6,
                compartment_layout={"rows": 2, "columns": 3},
                status="retrieving_bin",
            )
        )
        await session.commit()

    response = await client.put(
        "/api/storage/bins/tray-layout",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        json={"rows": 4, "columns": 4},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_tray_layout_rejects_out_of_range(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    response = await client.put(
        "/api/storage/bins/tray-layout",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        json={"rows": 0, "columns": 2},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_tray_layout_noop_when_unchanged(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    calls = {"n": 0}

    def _record(config):
        calls["n"] += 1

    monkeypatch.setattr(storage_router, "save_root_config", _record)

    response = await client.put(
        "/api/storage/bins/tray-layout",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        json={"rows": 2, "columns": 3},
    )

    assert response.status_code == 200, response.text
    # unchanged layout still persists idempotently
    assert calls["n"] == 1


def build_grid() -> StorageGrid:
    return StorageGrid(
        front_module=[
            Shelf(
                shelf_idx=0,
                divider_width=12.0,
                cells=[
                    Cell(
                        cell_idx=0,
                        status=CellStatus.AVAILABLE,
                        item=None,
                        calibration_point=None,
                        dimensions=Dimensions(width=55.0, height=90.0, length=400.0),
                    ),
                    Cell(
                        cell_idx=1,
                        status=CellStatus.AVAILABLE,
                        item=None,
                        calibration_point=None,
                        dimensions=Dimensions(width=55.0, height=90.0, length=400.0),
                    ),
                ],
            )
        ],
        back_module=[],
    )


def _worker(grid: StorageGrid):
    return SimpleNamespace(_robot=SimpleNamespace(_grid=grid))


def _patch_grid(monkeypatch: pytest.MonkeyPatch, grid: StorageGrid) -> None:
    monkeypatch.setattr(storage_router.robot_core, "robot_worker", _worker(grid))


@pytest.mark.anyio
async def test_assign_tray_creates_config_bin(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [])
    _patch_grid(monkeypatch, build_grid())
    saved: dict = {}
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: saved.update(config=config))

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        json={"barcode": "223456789012", "rows": 3, "columns": 4},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True}
    entry = saved["config"].storage.bins[0]
    assert entry.barcode == "223456789012"
    assert (entry.home_location.side, entry.home_location.shelf_idx, entry.home_location.cell_idx) == (
        "front_module", 0, 0
    )
    assert entry.compartment_layout is not None
    assert (entry.compartment_layout.rows, entry.compartment_layout.columns) == (3, 4)
    assert entry.max_items == 12
    # inner dimensions default to the cell's own dimensions
    assert entry.inner_dimensions == StorageBinDimensionsSettings(width=55.0, height=90.0, length=400.0)


@pytest.mark.anyio
async def test_assign_tray_missing_cell(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [])
    _patch_grid(monkeypatch, build_grid())
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 7, "cell_idx": 0},
        json={"barcode": "223456789012", "rows": 2, "columns": 2},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_assign_tray_already_home(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])  # already homed at front/0/1
    _patch_grid(monkeypatch, build_grid())
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        json={"barcode": "223456789012", "rows": 2, "columns": 2},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_assign_tray_barcode_reserved(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])  # reserves barcode BARCODE
    _patch_grid(monkeypatch, build_grid())
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        json={"barcode": BARCODE, "rows": 2, "columns": 2},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_assign_tray_barcode_is_item(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [])
    _patch_grid(monkeypatch, build_grid())
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    async with AsyncSessionLocal() as session:
        session.add(
            Item(
                barcode=BARCODE,
                name="Reserved",
                dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
            )
        )
        await session.commit()

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        json={"barcode": BARCODE, "rows": 2, "columns": 2},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_unassign_tray_removes_config_bin(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    saved: dict = {}
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: saved.update(config=config))

    response = await client.delete(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"success": True}
    assert saved["config"].storage.bins == []


@pytest.mark.anyio
async def test_unassign_tray_missing(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [])
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    response = await client.delete(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
    )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_unassign_tray_refuses_members(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [_bin_entry(2, 3)])
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    async with AsyncSessionLocal() as session:
        bin_row = StorageBin(
            barcode=BARCODE,
            side="front_module",
            shelf_idx=0,
            cell_idx=1,
            inner_dimensions={"width": 200.0, "height": 50.0, "length": 300.0},
            max_items=6,
            compartment_layout={"rows": 2, "columns": 3},
            status="at_home",
        )
        item = Item(
            barcode="333456789012",
            name="Member",
            dimensions={"width": 1.0, "height": 1.0, "length": 1.0},
        )
        session.add(bin_row)
        session.add(item)
        await session.flush()
        session.add(StorageBinItem(bin_id=bin_row.id, item_id=item.id, compartment=1))
        await session.commit()

    response = await client.delete(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
    )

    assert response.status_code == 409


@pytest.mark.anyio
async def test_assign_tray_reuses_deactivated_row(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _config_with_bins(monkeypatch, [])
    _patch_grid(monkeypatch, build_grid())
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: None)

    async with AsyncSessionLocal() as session:
        session.add(
            StorageBin(
                barcode="999012345678",
                side="front_module",
                shelf_idx=0,
                cell_idx=0,
                inner_dimensions={"width": 55.0, "height": 90.0, "length": 400.0},
                max_items=6,
                compartment_layout={"rows": 2, "columns": 3},
                status="at_home",
                active=False,
            )
        )
        await session.commit()

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
        json={"barcode": "888012345671", "rows": 4, "columns": 2},
    )

    assert response.status_code == 200, response.text

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(StorageBin).where(
                    StorageBin.side == "front_module",
                    StorageBin.shelf_idx == 0,
                    StorageBin.cell_idx == 0,
                )
            )
        ).scalars().first()
    # the deactivated row was re-pointed at the new barcode instead of colliding
    assert row is not None
    assert row.barcode == "888012345671"


def _tray_entry(barcode: str, cell_idx: int) -> StorageBinSettings:
    return StorageBinSettings(
        barcode=barcode,
        home_location=StorageBinLocationSettings(side="front_module", shelf_idx=0, cell_idx=cell_idx),
        inner_dimensions=StorageBinDimensionsSettings(width=55.0, height=90.0, length=400.0),
        max_items=6,
        compartment_layout=CompartmentLayoutSettings(rows=2, columns=3),
    )


@pytest.mark.anyio
async def test_assign_tray_barcode_held_by_another_removed_tray(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Two trays are removed; re-homing one of their barcodes at the other's former home
    # would duplicate the barcode on the stale row instead of colliding with the
    # unique home-cell constraint, so it is refused.
    base = get_root_config().model_copy(deep=True)
    base.storage.bins = [_tray_entry("111111111111", 0), _tray_entry("222222222222", 1)]
    state = {"config": base}
    monkeypatch.setattr(storage_router, "get_root_config", lambda: state["config"])
    monkeypatch.setattr(storage_router, "clear_config_cache", lambda: None)
    monkeypatch.setattr(storage_router, "save_root_config", lambda config: state.update(config=config))
    # sync_config reads the config from its own module namespace; keep it in step
    monkeypatch.setattr(bin_storage_core, "get_root_config", lambda: state["config"])
    _patch_grid(monkeypatch, build_grid())

    # create the database rows for the two configured trays
    async with AsyncSessionLocal() as session:
        await BinStorageService(session).sync_config()

    response = await client.delete(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 0},
    )
    assert response.status_code == 200, response.text
    response = await client.delete(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        "/api/storage/bins/tray",
        params={"side": "front_module", "shelf_idx": 0, "cell_idx": 1},
        json={"barcode": "111111111111", "rows": 2, "columns": 3},
    )

    assert response.status_code == 409, response.text

    # the stale row was not re-pointed at a barcode another row still holds
    async with AsyncSessionLocal() as session:
        barcodes = (
            await session.execute(select(StorageBin.barcode))
        ).scalars().all()
    assert barcodes == ["111111111111", "222222222222"]
