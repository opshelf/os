from abc import ABCMeta, abstractmethod

class AbstractScanner(metaclass=ABCMeta):

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def scan(self) -> str: ...

    @abstractmethod
    async def initialize(self) -> None: ...

