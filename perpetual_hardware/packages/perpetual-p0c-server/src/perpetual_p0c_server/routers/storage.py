from fastapi import APIRouter, Depends
from perpetual_p0c.storage import CellItem, CellLocationData, StorageGrid, StorageModuleCellLocation
from perpetual_p0c_server.core.robot.base import AbstractRobotWorker
from perpetual_p0c_server.core.storage import get_storage
from perpetual_p0c_server.core.robot import get_robot_worker
from typing import List, Optional

router = APIRouter()


@router.get("/storage")
async def get_storage_data(storage_grid: StorageGrid = Depends(get_storage)) -> StorageGrid:
    return storage_grid


@router.put("/storage")
async def update_storage(
    updates: StorageGrid,
    storage: StorageGrid = Depends(get_storage),
    worker: AbstractRobotWorker = Depends(get_robot_worker),
) -> StorageGrid:
    storage.update(updates)
    await worker.set_storage(updates)
    return storage


@router.get("/items")
async def get_items(storage: StorageGrid = Depends(get_storage)) -> List[CellLocationData]:
    return storage.get_all_cell_data()


@router.get("/locations")
async def get_locations(
    storage: StorageGrid = Depends(get_storage),
) -> List[StorageModuleCellLocation]:
    return storage.get_all_free_cells()


@router.post("/add_item")
async def store_item(
    location: StorageModuleCellLocation,
    item: Optional[CellItem] = None,
    storage: StorageGrid = Depends(get_storage),
) -> List[CellLocationData]:
    storage.store_item(location, item)
    storage.save()
    return storage.get_all_cell_data()


@router.delete("/delete_item")
async def delete_item(
    location: StorageModuleCellLocation, storage: StorageGrid = Depends(get_storage)
) -> List[CellLocationData]:
    storage.clear_item(location)
    storage.save()
    return storage.get_all_cell_data()
