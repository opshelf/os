import asyncio
from perpetual_p0c.storage import GS1Data
from .base import AbstractScanner
from typing import Optional, Tuple


class MockScanner(AbstractScanner):
    async def initialize(self) -> None:
        pass

    def location(self) -> str:
        return ""

    async def scan(self) -> Optional[Tuple[GS1Data, str]]:
        await asyncio.sleep(1)
        return None

    async def shutdown(self) -> None:
        pass
