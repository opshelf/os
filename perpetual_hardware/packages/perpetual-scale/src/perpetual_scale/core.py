from abc import ABCMeta, abstractmethod
from typing import Optional


class AbstractScale(metaclass=ABCMeta):
    @abstractmethod
    async def shutdown(self) -> None: ...

    @abstractmethod
    async def read(self) -> Optional[float]: ...

    @abstractmethod
    async def initialize(self) -> None: ...
