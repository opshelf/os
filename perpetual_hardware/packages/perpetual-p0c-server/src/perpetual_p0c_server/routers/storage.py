from fastapi import APIRouter, Depends
from perpetual_p0c.storage import CellItem, CellLocationData, StorageGrid, StorageModuleCellLocation
from perpetual_p0c_server.core.storage import get_storage
from typing import List

router = APIRouter()

@router.get("/storage")
async def get_storage_data(storage_grid: StorageGrid = Depends(get_storage)) -> StorageGrid:
    return storage_grid

@router.put("/storage")
async def update_storage(
    updates: StorageGrid,
    storage: StorageGrid = Depends(get_storage)
) -> StorageGrid:
    storage.update(updates)
    return storage

@router.get("/items")
async def get_items(
    storage: StorageGrid = Depends(get_storage)
) -> List[CellLocationData]:
    print(storage.get_all_cell_data())
    return storage.get_all_cell_data()

@router.get("/locations")
async def get_locations(
    storage: StorageGrid = Depends(get_storage)
) -> List[StorageModuleCellLocation]:
    return storage.get_all_free_cells()


@router.post("/add_item")
async def store_item(
    location: StorageModuleCellLocation,
    item: CellItem,
    storage: StorageGrid = Depends(get_storage)
) -> List[CellLocationData]:
    storage.store_item(location, item)
    storage.save()
    return storage.get_all_cell_data()

@router.delete("/delete_item")
async def delete_item(
    location: StorageModuleCellLocation,
    storage: StorageGrid = Depends(get_storage)
) -> List[CellLocationData]:
    storage.clear_item(location)
    storage.save()
    return storage.get_all_cell_data()
