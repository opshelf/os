from typing import Optional
from perpetual_p0c.storage import StorageGrid, StorageModuleCellLocation, Tray
import asyncio
import random
from perpetual_p0c.robot import P0C
from perpetual_p0c.storage import StorageModuleSide, StorageModuleTrayLocation, StorageGrid
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.configuration import p0_robot_config

async def main() -> None:
    can_bus = await get_can_bus()
    robot = P0C(can_bus, p0_robot_config)
    await robot.initialize()
    await robot.home()
    await robot.idle()
    with open("storage.json") as f:
        storage_module = StorageGrid.model_validate_json(f.read())
        robot.storage = storage_module
    await robot.receive_item(
        StorageModuleCellLocation(
            side=StorageModuleSide.RIGHT,
            shelf_idx=1,
            tray_idx=1,
            slot_idx=1
    ))

if __name__ == '__main__':
    asyncio.run(main())
