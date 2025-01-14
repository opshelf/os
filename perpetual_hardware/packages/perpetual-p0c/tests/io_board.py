from perpetual_p0c.utils import get_can_bus
from perpetual_p0c.io_board import IoController
from perpetual_p0c.funnel import Funnel
from perpetual_p0c.configuration import p0_funnel_config, p0_workstation_configuration

import asyncio

from perpetual_p0c.workstation import Workstation

async def main() -> None:
    can_bus = await get_can_bus()
    controller = IoController(can_bus)
    funnel = Funnel(can_bus, p0_funnel_config)
    workstation = Workstation(can_bus, p0_workstation_configuration)
    await funnel.initialize()
    #await funnel.start_receive_item()
    #await workstation.start_input_belt()
    while True:
        sensors = await controller.get_sensors()
        if sensors:
            print(sensors)



if __name__ == "__main__":
    asyncio.run(main())
