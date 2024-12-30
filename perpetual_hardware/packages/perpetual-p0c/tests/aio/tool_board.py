
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.tool_board import ToolBoard

import asyncio
#from my_async_module import my_async_function

async def main() -> None:
    can_bus = await get_can_bus()
    tool = ToolBoard(can_bus)
    state = await tool.get_state()
    print(state)



if __name__ == "__main__":
    asyncio.run(main())
