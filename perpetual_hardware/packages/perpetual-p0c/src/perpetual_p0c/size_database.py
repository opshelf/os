from pydantic import BaseModel
from perpetual_p0c.storage import GS1Data, Dimensions
from typing import Dict, Optional


class SizeDatabase(BaseModel):
    items: Dict[str, Dimensions] = {}

    def gs1_to_str(self, data: GS1Data) -> str:
        return data.gtin + data.expiry + data.lot + data.sn

    def get_item_size(self, data: GS1Data) -> Optional[Dimensions]:
        return self.items.get(self.gs1_to_str(data))

    def add_item_size(self, data: GS1Data, dimensions: Dimensions) -> None:
        self.items[self.gs1_to_str(data)] = dimensions

    def load_from_file(self, filename: str) -> None:
        with open(filename) as f:
            result = self.model_validate_json(f.read())
            self.items = result.items

    def save_to_file(self, filename: str) -> None:
        with open(filename, "w+") as f:
            f.write(self.model_dump_json())
