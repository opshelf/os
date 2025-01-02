from pydantic import BaseModel
from perpetual_p0c.storage import GS1Data, Dimensions
from typing import Dict, Optional, Union


class SizeDatabase(BaseModel):
    items: Dict[str, Dimensions] = {}
    filename: str = "size.json"

    def gs1_to_str(self, data: GS1Data) -> str:
        return "-".join([data.gtin, data.expiry, data.lot, data.sn])

    def get_item_size(self, data: Union[GS1Data, str]) -> Optional[Dimensions]:
        if isinstance(data, GS1Data):
            return self.items.get(self.gs1_to_str(data))
        else:
            return self.items.get(data)

    def add_item_size(self, data: GS1Data, dimensions: Dimensions) -> None:
        self.items[self.gs1_to_str(data)] = dimensions

    def remove_item_size(self, data: GS1Data) -> None:
        self.items.pop(self.gs1_to_str(data), None)

    def load_from_file(self, filename: str) -> None:
        with open(filename) as f:
            result = self.model_validate_json(f.read())
            self.items = result.items

    def save_to_file(self, filename: str) -> None:
        with open(filename, "w+") as f:
            f.write(self.model_dump_json())

    def save(self) -> None:
        self.save_to_file(self.filename)
