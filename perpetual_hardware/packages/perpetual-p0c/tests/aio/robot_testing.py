from perpetual_p0c.robot import P0C
import asyncio
from perpetual_p0c.storage import CellItem, Dimensions, StorageModuleCellLocation, StorageModuleSide, StorageModuleTrayLocation, Tray, CalibrationCoordinate
from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.configuration import p0_robot_config

async def main() -> None:
    can_bus = await get_can_bus()
    robot = P0C(can_bus, p0_robot_config)
    await robot.initialize()
    await robot.stop()
    await robot.home()
    """
    await robot.funnel.start_output_transfer()
    while True:
        #await asyncio.sleep(5)
        await robot.funnel.output_transfer_conveyor.wait_for_move()
    """

    #input()
    #await robot.home()
    #input()
    #await robot.service_high()
    #await robot.funnel.opposing_funnel.move_to_position(340)
    #await robot.funnel.opposing_funnel.wait_for_move()
    #input()
    await robot.funnel.opposing_funnel.home()
    while True:
        await robot.funnel.opposing_funnel.move_to_position(340)
        await robot.funnel.opposing_funnel.wait_for_move()
        await robot.funnel.opposing_funnel.move_to_position(0)
        await robot.funnel.opposing_funnel.wait_for_move()
    #while True:
    #    tray_loaded = await robot.tool.check_tray_loaded()
    #    print(tray_loaded)
    #    await asyncio.sleep(.1)
    """
    try:
        await robot.run_output_transfer()
    finally:
        await robot.stop()
    """

    #try:
    #    await robot.run_input_transfer(
    #        Tray(
    #            width=150,
    #            height=200,
    #            calibration_point=CalibrationCoordinate(x=85, y=24, z=480),
    #            slots=[],
    #        ),
    #        0
    #    )
    #finally:
    #    await robot.stop()
    """
    while True:
        state = await robot._io.get_sensors()
        print(state)
        await asyncio.sleep(1)
    """









if __name__ == "__main__":
    asyncio.run(main())
