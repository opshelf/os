from abc import ABCMeta, abstractmethod
from typing import List
from perpetual_p0c.robot import RobotCommands, RobotResponses


class AbstractRobotClient(metaclass=ABCMeta):
    @abstractmethod
    async def send_command(self, command: RobotCommands) -> None: ...

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
