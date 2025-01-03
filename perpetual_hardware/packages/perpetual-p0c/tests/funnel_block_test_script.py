from perpetual_p0c.configuration import p0_funnel_config
from perpetual_trinamic.aio.trinamic_axis import TrinamicAxis
from perpetual_p0c.utils import get_can_bus
import asyncio

async def main() -> None:
    can_bus = await get_can_bus()
    slot1 = TrinamicAxis(
        can_bus,
        p0_funnel_config.slot_1.configuration,
        module_id=p0_funnel_config.slot_1.can_id,
    )
    slot2 = TrinamicAxis(
        can_bus,
        p0_funnel_config.slot_2.configuration,
        module_id=p0_funnel_config.slot_2.can_id,
    )
    slot3 = TrinamicAxis(
        can_bus,
        p0_funnel_config.slot_3.configuration,
        module_id=p0_funnel_config.slot_3.can_id,
    )
    for _ in range(0, 10):
        print("Press Enter to Home the Wall Blocks at the Same Time")
        input()
        home_1 = asyncio.create_task(slot1.home())
        home_2 = asyncio.create_task(slot2.home())
        home_3 = asyncio.create_task(slot3.home())
        await asyncio.gather(home_1, home_2, home_3) 
        print("Press Enter to Home the Wall Blocks Individually")
        input()
        await slot1.home()
        await slot2.home()
        await slot3.home()
        print("Press Enter to Move Wall Blocks in Increments of 30mm 10 times.")
        input()
        for i in range(0, 11):
            await slot1.move_to_position(30*i)
            await slot2.move_to_position(30*i)
            await slot3.move_to_position(30*i)
            await slot1.wait_for_move()
            await slot2.wait_for_move()
            await slot3.wait_for_move()
        print("Press Enter to Move Wall Blocks in Increments of 30mm 10 times.")
        input()
        for i in range(0, 11):
            await slot1.move_to_relative_position(15)
            await slot2.move_to_relative_position(15)
            await slot3.move_to_relative_position(15)
            await slot1.wait_for_move()
            await slot2.wait_for_move()
            await slot3.wait_for_move()

async def slot_1_test() -> None:
    can_bus = await get_can_bus()
    slot1 = TrinamicAxis(
        can_bus,
        p0_funnel_config.slot_3.configuration,
        module_id=p0_funnel_config.slot_3.can_id,

    )
    await slot1.home()
    #await slot1.safety_off()
    #for _ in range(0, 11):
    #    await slot1.move_to_relative_position(10)
    #    await slot1.wait_for_move()
    #await slot1.home()
    #for i in range(0, 11):
    #    await slot1.move_to_position(30*i)
    #    await slot1.wait_for_move()
    #print("Press Enter to Move Wall Blocks in Increments of 30mm 10 times.")
    #for i in range(0, 11):
    #    await slot1.move_to_position(15*i)
    #    await slot1.wait_for_move()

if __name__ == "__main__":
    asyncio.run(main())
    #asyncio.run(slot_1_test())

