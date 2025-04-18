from abc import ABCMeta, abstractmethod
from typing import Optional

class AbstractScanner(metaclass=ABCMeta):

    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def scan(self) -> Optional[str]: ...

    @abstractmethod
    async def initialize(self) -> None: ...

