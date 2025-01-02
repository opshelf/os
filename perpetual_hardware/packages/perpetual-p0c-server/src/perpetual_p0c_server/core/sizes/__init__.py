from perpetual_p0c.size_database import SizeDatabase
import os
from typing import Optional
from perpetual_p0c_server.settings import get_settings

size_database: Optional[SizeDatabase] = None


def init_file_sizes() -> None:
    global size_database
    settings = get_settings()
    if os.path.isfile(settings.sizes.filename):
        with open(settings.sizes.filename) as f:
            size_database = SizeDatabase.model_validate_json(f.read())
    else:
        with open(settings.storage.filename, "w+") as f:
            default_size_db = SizeDatabase()
            f.write(default_size_db.model_dump_json())
            size_database = default_size_db


def get_size_db() -> SizeDatabase:
    if size_database:
        return size_database
    raise ValueError("Size Database was Not Initialized")
