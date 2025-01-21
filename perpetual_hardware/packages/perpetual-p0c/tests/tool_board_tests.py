from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.tool_board import ToolBoard
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    tool = ToolBoard(can_bus)
    while True:
        sensors = await tool.get_state()
        if sensors:
            print(sensors)



if __name__ == "__main__":
    asyncio.run(main())
