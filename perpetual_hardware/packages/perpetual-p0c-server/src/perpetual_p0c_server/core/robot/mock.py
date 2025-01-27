from typing import Optional
from perpetual_p0c.robot import (
    AbstractRobot,
    AckResponse,
    InitializeCommand,
    RobotConfiguration,
    RobotCommands,
    RobotState,
    RobotStatus,
    StoreCommand,
    StopCommand,
    HomeCommand,
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
                    status=ResponseStatus.COMPLETED,
                    command=command,
                )
            case RetrieveCommand():
                await asyncio.sleep(random.random() * 10)
                return RetrieveResponse(status=ResponseStatus.COMPLETED, command=command)
            case HomeCommand():
                await asyncio.sleep(random.random() * 20)
                return AckResponse(
                    command=command,
                    message="homed",
                    status=ResponseStatus.COMPLETED,
                )
            case StopCommand():
                await asyncio.sleep(random.random() * 2)
                return AckResponse(
                    command=command,
                    message="stopped",
                    status=ResponseStatus.COMPLETED,
                )
            case InitializeCommand():
                await asyncio.sleep(random.random() * 2)
                return AckResponse(
                    command=command,
                    message="initialize",
                    status=ResponseStatus.COMPLETED,
                )
            case _:
                raise Exception("Invalid Robot Command")

    def holding_tray(self) -> bool:
        return False

    async def status(self) -> Optional[RobotStatus]:
        return None
