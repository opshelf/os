from typing import Optional
from perpetual_p0c.storage import StorageGrid
import asyncio
import random
from perpetual_p0c.robot import P0C
from perpetual_p0c.storage import StorageModuleSide, StorageModuleTrayLocation
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.configuration import p0_robot_config

async def main() -> None:
    storage_module: Optional[StorageGrid] = None
    with open("storage.json") as f:
        storage_module = StorageGrid.model_validate_json(f.read())
    can_bus = await get_can_bus()
    robot = P0C(can_bus, p0_robot_config)
    if storage_module:
        robot.storage = storage_module
    await robot.initialize()
    await robot.home()
    while True:
        tray_idx = random.randint(0, 2)
        shelf_idx = random.randint(0, 2)
        loc = StorageModuleTrayLocation(
            side=StorageModuleSide.RIGHT,
            shelf_idx=shelf_idx,
            tray_idx=tray_idx
        )
        await robot.pull_tray(loc)
        await robot.place_tray(loc)
        await robot.service_high()

if __name__ == '__main__':
    asyncio.run(main())
