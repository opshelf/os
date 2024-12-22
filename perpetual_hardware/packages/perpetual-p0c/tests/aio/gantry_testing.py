from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.aio.gantry import Gantry
from perpetual_p0c.configuration import p0_gantry_config

import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    gantry = Gantry(can_bus, p0_gantry_config)
    await gantry.y_axis.home()
    await gantry.home()



if __name__ == "__main__":
    asyncio.run(main())
