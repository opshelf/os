from abc import abstractmethod, ABCMeta
from perpetual_p0c_server.core.events import (
    InputDoorEvent,
    OutputDoorEvent,
    WorkstationEvents
)
import logging

class AbstractWorkstation(metaclass=ABCMeta):

    @abstractmethod
    async def close_all_outputs(self) -> None: ...

    @abstractmethod
    async def open_all_outputs(self) -> None: ...

    @abstractmethod
    async def open_output(self, output_box: int) -> None: ...

    @abstractmethod
    async def close_output(self, output_box: int) -> None: ...

    @abstractmethod
    async def open_input(self) -> None: ...

    @abstractmethod
    async def close_input(self) -> None: ...

    async def handle_command(self, command: WorkstationEvents) -> None:
        logging.info(f"Workstation Command: {command}")
        match command:
            case InputDoorEvent(payload=payload):
                if payload.state == "open":
                    await self.open_input()
                else:
                    await self.close_input()
            case OutputDoorEvent(payload=payload):
                if payload.state == "open":
                    await self.open_output(payload.output_box)
                else:
                    await self.close_output(payload.output_box)
            case _:
                raise Exception("Invalid Workstation Command")

