"""Inventory and bin changes made while the API runs must reach the live robot grid."""

from types import SimpleNamespace

import pytest

from openshelf_os.config.settings import (
    CompartmentLayoutSettings,
    StorageBinDimensionsSettings,
    StorageBinLocationSettings,
    StorageBinSettings,
)
from openshelf_os.robot.core import Robot
from openshelf_os.storage import (
    CalibrationCoordinate,
    Cell,
    CellItem,
    CellStatus,
    Dimensions,
    Shelf,
    StorageGrid,
)
from openshelf_os_server.core import bin_storage
from openshelf_os_server.database.models import Item, ItemLocation
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.routers import storage as storage_router
from openshelf_os_server.settings import get_root_config


def _cell(cell_idx: int, barcode: str | None) -> Cell:
    dimensions = Dimensions(width=100.0, length=100.0, height=100.0)
    return Cell(
        cell_idx=cell_idx,
        status=CellStatus.OCCUPIED if barcode else CellStatus.AVAILABLE,
        item=CellItem(barcode=barcode, dimensions=dimensions) if barcode else None,
        calibration_point=CalibrationCoordinate(x=10.0, y=20.0),
        dimensions=dimensions,
    )


def _robot(grid: StorageGrid | None = None) -> Robot:
    robot = Robot.__new__(Robot)
    robot._grid = grid
    robot._reserved_cell_locations = set()
    return robot


@pytest.mark.anyio
async def test_delete_clears_the_deleted_item_from_the_live_grid(monkeypatch):
    # A deleted item left in the grid stays listed on a bin home, which the status overlay
    # marks occupied, and every further delete of it is a 404 (Sentry OPENSHELF-API-B2).
    deleted, kept = "CAL-900000000901", "900000000902"
    grid = StorageGrid(
        front_module=[Shelf(shelf_idx=0, cells=[_cell(0, deleted), _cell(1, kept)])],
        back_module=[],
    )
    monkeypatch.setattr(
        storage_router.robot_core, "robot_worker", SimpleNamespace(_robot=_robot(grid))
    )
    async with AsyncSessionLocal() as db:
        for idx, barcode in enumerate((deleted, kept)):
            item = Item(barcode=barcode, name=barcode, dimensions={"width": 1, "height": 1, "length": 1})
            db.add(item)
            await db.flush()
            db.add(ItemLocation(item_id=item.id, side="front_module", shelf_idx=0, cell_idx=idx))
        await db.commit()

        await storage_router.delete_item(
            storage_router.BarcodeRequest(barcode=deleted),
            session=db,
            current_user=SimpleNamespace(id=1, role=SimpleNamespace(name="admin")),
        )

    removed, untouched = grid.front_module[0].cells
    assert removed.status == CellStatus.AVAILABLE and removed.item is None
    assert untouched.status == CellStatus.OCCUPIED and untouched.item.barcode == kept


@pytest.mark.anyio
async def test_bin_config_sync_replaces_the_live_robot_reservations(monkeypatch):
    # A bin assigned while the API runs must be reserved at once, or an explicit store can
    # target the cell its bin sits in; a removed bin must stop reserving its old home.
    config = get_root_config().model_copy(deep=True)
    config.storage.bins = [
        StorageBinSettings(
            barcode="900000000903",
            home_location=StorageBinLocationSettings(side="front_module", shelf_idx=4, cell_idx=2),
            inner_dimensions=StorageBinDimensionsSettings(width=85.0, height=85.0, length=145.0),
            max_items=6,
            compartment_layout=CompartmentLayoutSettings(rows=3, columns=2),
        )
    ]
    monkeypatch.setattr(bin_storage, "get_root_config", lambda: config)
    robot = _robot()
    robot._reserved_cell_locations = {("back_module", 9, 9)}
    monkeypatch.setattr(
        storage_router.robot_core, "robot_worker", SimpleNamespace(_robot=robot)
    )

    async with AsyncSessionLocal() as db:
        await bin_storage.BinStorageService(db).sync_config()

    assert ("front_module", 4, 2) in robot._reserved_cell_locations
    assert ("back_module", 9, 9) not in robot._reserved_cell_locations
