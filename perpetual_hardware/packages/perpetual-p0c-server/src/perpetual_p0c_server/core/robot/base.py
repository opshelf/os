from abc import ABCMeta, abstractmethod
from typing import List, Union
from perpetual_p0c.robot import RobotCommands, RobotConfiguration, RobotResponses
from perpetual_p0c.storage import StorageGrid
from perpetual_p0c_server.core.events import StorageEvents


class AbstractRobotClient(metaclass=ABCMeta):
    @abstractmethod
    async def send_command(self, command: Union[RobotCommands, StorageEvents]) -> None: ...

    @abstractmethod
    async def send_emergency_command(self, command: RobotCommands) -> None: ...

    @abstractmethod
    async def get_queued_commands(self) -> List[RobotCommands]: ...

    @abstractmethod
    async def handle_response(self, response: RobotResponses) -> None: ...


class AbstractRobotWorker(metaclass=ABCMeta):
    @abstractmethod
    async def get_client(self) -> AbstractRobotClient: ...

    @abstractmethod
    async def queue_command(self, command: RobotCommands) -> None: ...

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def clear_commands(self) -> None: ...

    @abstractmethod
    async def set_storage(self, storage: StorageGrid) -> None: ...

    @abstractmethod
    async def get_storage(self) -> StorageGrid: ...

    @abstractmethod
    def get_configuration(self) -> RobotConfiguration: ...

    @abstractmethod
    async def set_configuration(self, configuration: RobotConfiguration) -> None: ...
