from abc import ABCMeta, abstractmethod


class AbstractScale(metaclass=ABCMeta):
    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def read(self) -> float: ...

    @abstractmethod
    async def initialize(self) -> None: ...
