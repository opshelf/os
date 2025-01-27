from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.tool_board import IoBoard, ToolBoard
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    tool = ToolBoard(can_bus)
    while True:
        sensors = await tool.get_state()
        if sensors:
            print(sensors)
        await asyncio.sleep(1)
        await tool.set_carriage(IoBoard(
            r1=True
        ))
        await asyncio.sleep(1)
        await tool.set_carriage(IoBoard(
            r2=True
        ))
        await asyncio.sleep(1)
        await tool.set_carriage(IoBoard(
            r2=True
        ))
        await tool.set_carriage(IoBoard(
        ))
        await asyncio.sleep(10)



if __name__ == "__main__":
    asyncio.run(main())
