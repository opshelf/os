from pydantic import BaseModel
from typing import Optional, Dict, Any, Literal


class BaseCommand(BaseModel):
    command: (
        Literal["STORE_ITEM"] | Literal["RETRIEVE_ITEM_LOCATION"] | Literal["RETRIEVE_ITEM_GS1"]
    )
    payload: Optional[Dict[Any, Any]]


class Status(BaseModel):
    msg: str


#    def store_item(self, item: CellItem) -> None:
#        location = self._storage.get_next_free_location(item)
#        if location:
#            self._receive_item(location)
#            self._storage.store_item(location, item)
#            self.save_storage()
#        else:
#            raise Exception("No Valid Location")
#
#    def store_item_gs1_data(self, data: GS1Data) -> None:
#        item_size = self._sizes.get_item_size(data)
#        if item_size:
#            item = CellItem(
#                height=item_size.height,
#                width=max(item_size.width, item_size.length),
#                gs1_data=data
#            )
#            location = self._storage.get_next_free_location(item)
#            if location:
#                self._receive_item(location)
#                self._storage.store_item(location, item)
#                self.save_storage()
#            else:
#                raise Exception("No Valid Location")
#        else:
#            raise Exception("No Known Size")
#
#    def retrieve_item_by_location(self, location: StorageModuleCellLocation) -> None:
#        self._drop_off_item(location)
#        self._storage.clear_item(location)
#        self.save_storage()
#
#    def retrieve_item_by_gtin(self, data: GS1Data) -> None:
#        location = self._storage.get_item_location(data)
#        self._drop_off_item(location)
#        self._storage.clear_item(location)
#        self.save_storage()
