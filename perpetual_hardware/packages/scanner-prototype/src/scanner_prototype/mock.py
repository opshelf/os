import os
from perpetual_p0c.robot import RobotConfiguration
from perpetual_p0c.size_database import SizeDatabase
from perpetual_p0c.storage import CellItem, GS1Data, StorageModuleCellLocation, StorageGrid


class MockP0C:
    def __init__(self, config: RobotConfiguration):
        self._storage = StorageGrid(
            left_module=[],
            right_module=[],
        )
        self._config = config
        self._sizes = SizeDatabase()
        self.load_storage()
        self.load_sizes()

    def save_sizes(self) -> None:
        self._sizes.save_to_file(self._config.size_file)

    def load_sizes(self) -> None:
        if os.path.exists(self._config.size_file):
            self._sizes.load_from_file(self._config.size_file)
        else:
            self.save_sizes()

    def load_storage(self) -> None:
        if os.path.exists(self._config.storage_file):
            with open(self._config.storage_file) as f:
                self._storage = StorageGrid.model_validate_json(f.read())
        else:
            self.save_storage()

    def save_storage(self) -> None:
        self._storage.save_to_file(self._config.storage_file)

    def shutdown(self) -> None:
        pass

    @property
    def config(self) -> RobotConfiguration:
        return self._config

    @property
    def size_db(self) -> SizeDatabase:
        return self._sizes

    @property
    def storage(self) -> StorageGrid:
        return self._storage
