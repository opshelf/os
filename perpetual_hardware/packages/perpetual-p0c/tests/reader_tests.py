from typing import Dict
from perpetual_p0c.utils import get_bus
from perpetual_p0c.configuration import p0_funnel_config, p0_workstation_configuration

import asyncio


async def main() -> None:
    can_bus = get_bus()
    counter: Dict[int, int] = {}
    while True:
        msg = can_bus.recv()
        if msg and msg.is_error_frame:
            raise Exception(f"Error Frame Encounterd: {msg}")
        elif msg:
            if msg.arbitration_id in counter:
                counter[msg.arbitration_id] += 1
            else:
                counter[msg.arbitration_id] = 1
        print(counter)


if __name__ == '__main__':
    asyncio.run(main())
