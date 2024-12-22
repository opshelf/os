from perpetual_p0c.aio.robot import P0C
import asyncio
from perpetual_p0c.storage import CellItem, Dimensions, StorageModuleCellLocation, StorageModuleSide, StorageModuleTrayLocation
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.configuration import p0_robot_config

async def main() -> None:
    can_bus = await get_can_bus()
    robot = P0C(can_bus, p0_robot_config)
    await robot.stop()
    await robot.idle()
    #await robot.home_y_first()
    input()
    await robot.store_item(CellItem(
        dimensions=Dimensions(
            length=40,
            height=100,
            width=40,
        )
    ))
    await robot.return_current_tray()
    """
    await robot._pull_tray(
        StorageModuleTrayLocation(
            side=StorageModuleSide.RIGHT,
            shelf_idx=2,
            tray_idx=0
        )
    )
    """





if __name__ == "__main__":
    asyncio.run(main())
