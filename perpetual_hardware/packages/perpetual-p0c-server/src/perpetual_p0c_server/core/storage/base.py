from abc import ABCMeta, abstractmethod
from perpetual_p0c.storage import CellItem, StorageModuleCellLocation, StorageModuleTrayLocation, CellLocationData, CalibrationCoordinate, GS1Data
from typing import List, Optional

class AbstractStorageModule(metaclass=ABCMeta):

    @abstractmethod
    def get_next_free_location(self, item: CellItem) -> Optional[StorageModuleCellLocation]:
        ...

    @abstractmethod
    def get_tray_coordinate(
        self, location: StorageModuleTrayLocation | StorageModuleCellLocation
    ) -> CalibrationCoordinate:
        ...

    @abstractmethod
    def get_coordinate(self, location: StorageModuleCellLocation) -> CalibrationCoordinate:
        ...

    @abstractmethod
    def store_item(self, location: StorageModuleCellLocation, item: CellItem) -> None:
        ...

    @abstractmethod
    def clear_item(self, location: StorageModuleCellLocation) -> None:
        ...

    @abstractmethod
    def clear_all_items(self) -> None:
        ...

    @abstractmethod
    def get_all_items(self) -> List[StorageModuleCellLocation]:
        ...

    @abstractmethod
    def get_all_cell_data(self) -> List[CellLocationData]:
        ...

    @abstractmethod
    def get_all_items_gs1(self) -> List[CellItem]:
        ...

    @abstractmethod
    def get_item_location(self, data: GS1Data) -> StorageModuleCellLocation:
        ...
