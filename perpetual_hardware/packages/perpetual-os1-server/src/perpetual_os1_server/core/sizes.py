from pydantic import BaseModel
from perpetual_os1.storage import Dimensions, Json
from typing import Dict, Iterable, Optional, Union, Mapping
import os
from typing import Optional
from perpetual_os1_server.settings import get_settings



class ItemInformation(BaseModel):
    size: Dimensions
    data: Json


class SizeDatabase(BaseModel):
    items: Dict[str, ItemInformation] = {}
    filename: str = "size.json"

    def get_item_size(self, data: str) -> Optional[ItemInformation]:
        return self.items.get(data)

    def add_item_size(self, upca: str, data: Json, dimensions: Dimensions) -> None:
        print(data, dimensions)
        self.items[upca] = ItemInformation(size=dimensions, data=data)
        print(self.items)

    def remove_item_size(self, upca: str) -> None:
        self.items.pop(upca, None)

    def load_from_file(self, filename: str) -> None:
        with open(filename) as f:
            result = self.model_validate_json(f.read())
            self.items = result.items

    def save_to_file(self, filename: str) -> None:
        with open(filename, "w+") as f:
            f.write(self.model_dump_json())

    def save(self) -> None:
        self.save_to_file(self.filename)


size_database: Optional[SizeDatabase] = None


def init_file_sizes() -> None:
    global size_database
    settings = get_settings()
    if os.path.isfile(settings.sizes.filename):
        with open(settings.sizes.filename) as f:
            size_database = SizeDatabase.model_validate_json(f.read())
    else:
        with open(settings.sizes.filename, "w+") as f:
            default_size_db = SizeDatabase(filename=settings.sizes.filename)
            f.write(default_size_db.model_dump_json())
            size_database = default_size_db


def get_size_db() -> SizeDatabase:
    if size_database:
        return size_database
    raise ValueError("Size Database was Not Initialized")
