from fastapi import APIRouter
from .types import StorageGrid
import os

router = APIRouter()

STORAGE_FILE = "storage.json"

@router.get("/storage")
async def get_storage() -> StorageGrid:
    if os.path.isfile(STORAGE_FILE):
        with open(STORAGE_FILE) as f:
            return StorageGrid.model_validate_json(f.read())
    else:
        with open(STORAGE_FILE, "w+") as f:
            default_grid = StorageGrid(left_module=[], right_module=[])
            f.write(default_grid.model_dump_json())
            return default_grid

@router.put("/storage")
async def update_storage(
    storage: StorageGrid
) -> StorageGrid:
    with open(STORAGE_FILE, "w+") as f:
        f.write(storage.model_dump_json())
        return storage
