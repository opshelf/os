from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.io_board import IoController
from perpetual_p0c.funnel import Funnel
from perpetual_p0c.tool_board import ToolBoard
from perpetual_p0c.configuration import p0_funnel_config, p0_workstation_configuration

import asyncio

from perpetual_p0c.workstation import Workstation

async def main() -> None:
    can_bus = await get_can_bus()
    controller = IoController(can_bus)
    funnel = Funnel(can_bus, p0_funnel_config, controller)
    workstation = Workstation(can_bus, p0_workstation_configuration)
    #tool = ToolBoard(can_bus)


    await funnel.stop()
    await workstation.stop()
    input()
    await funnel.initialize()
    await workstation.initialize()
    #await funnel.start_receive_item()
    await funnel.start_belt()
    await workstation.start_input_belt()
    idx = 0
    state = False
    while True:
        sensors = await controller.get_sensors()
        if sensors:
            print(sensors)
        #sensors = await tool.get_state()
        #if sensors:
        #    print(sensors)
        idx += 1
        if idx % 1000 == 0:
            idx = 0
            if not state:
                await funnel.stop()
                await workstation.stop()
                state = True
            else:
                await funnel.start_receive_item()
                await workstation.start_input_belt()
                state = False




if __name__ == "__main__":
    asyncio.run(main())
