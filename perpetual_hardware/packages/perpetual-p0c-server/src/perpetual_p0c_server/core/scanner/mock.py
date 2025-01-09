import asyncio
from perpetual_p0c.storage import GS1Data
from .base import AbstractScanner
from typing import Optional


class MockScanner(AbstractScanner):
    async def initialize(self) -> None:
        pass

    async def scan(self) -> Optional[GS1Data]:
        await asyncio.sleep(1)
        return None

    async def shutdown(self) -> None:
        pass
