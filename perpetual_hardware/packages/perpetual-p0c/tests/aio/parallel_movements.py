from perpetual_p0c.gantry import GantryCoordinate
from perpetual_p0c.storage import StorageModuleSide
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.gantry import Gantry
from perpetual_p0c.tool import Tool
from perpetual_p0c.configuration import p0_gantry_config, p0_tool_config

import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    gantry = Gantry(can_bus, p0_gantry_config)
    tool = Tool(can_bus, p0_tool_config)
    await tool.home()
    await gantry.home()

    puller_move_task = asyncio.create_task(tool.carriage_safe(StorageModuleSide.RIGHT))
    gantry_move_task = asyncio.create_task(gantry.move_coordinate(GantryCoordinate(x=100, y=500)))
    await puller_move_task
    await gantry_move_task





if __name__ == "__main__":
    asyncio.run(main())
