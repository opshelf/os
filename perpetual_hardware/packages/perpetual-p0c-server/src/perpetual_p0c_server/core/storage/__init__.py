from perpetual_p0c.storage import StorageGrid
import os
from typing import Optional
from perpetual_p0c_server.settings import get_settings

storage_module: Optional[StorageGrid] = None


def init_file_storage() -> None:
    global storage_module
    settings = get_settings()
    if os.path.isfile(settings.storage.filename):
        with open(settings.storage.filename) as f:
            storage_module = StorageGrid.model_validate_json(f.read())
    else:
        with open(settings.storage.filename, "w+") as f:
            default_grid = StorageGrid(left_module=[], right_module=[])
            f.write(default_grid.model_dump_json())
            storage_module = default_grid


def get_storage() -> StorageGrid:
    if storage_module:
        return storage_module
    raise ValueError("Storage Module was Not Initialized")
