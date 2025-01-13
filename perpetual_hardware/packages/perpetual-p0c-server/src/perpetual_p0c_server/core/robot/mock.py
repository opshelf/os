from perpetual_p0c.robot import (
    AbstractRobot,
    RobotConfiguration,
    RobotCommands,
    RobotState,
    StoreCommand,
    RetrieveCommand,
    RetrieveResponse,
    StorageResponse,
    ResponseStatus,
    RobotResponses,
)
from perpetual_p0c.storage import StorageGrid
import random
import asyncio


class MockRobot(AbstractRobot):
    def __init__(self, configuration: RobotConfiguration) -> None:
        self._storage = StorageGrid(left_module=[], right_module=[])
        self._configuration = configuration
        self._state = RobotState()

    @property
    def state(self) -> RobotState:
        return self._state

    @property
    def storage(self) -> StorageGrid:
        return self._storage

    @storage.setter
    def storage(self, storage: StorageGrid) -> None:
        self._storage = storage

    @property
    def configuration(self) -> RobotConfiguration:
        return self._configuration

    async def set_configuration(self, configuration: RobotConfiguration) -> None:
        self._configuration = configuration

    async def handle_command(self, command: RobotCommands) -> RobotResponses:
        match command:
            case StoreCommand():
                await asyncio.sleep(random.random() * 10)
                return StorageResponse(
                    **command.model_dump(),
                    status=ResponseStatus.COMPLETED,
                )
            case RetrieveCommand():
                await asyncio.sleep(random.random() * 10)
                return RetrieveResponse(status=ResponseStatus.COMPLETED, command=command)
            case _:
                raise Exception("Invalid Robot Command")
