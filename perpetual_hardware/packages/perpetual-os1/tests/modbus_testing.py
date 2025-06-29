from perpetual_waveshare.digital import DigitalIOController
from perpetual_os1.curtain import IntelligentGrating
from perpetual_os1.utils import get_modbus_client
import asyncio

async def main() -> None:
    client = await get_modbus_client()
    io = DigitalIOController(client)
    sizing_curtain = IntelligentGrating(client, address=0x40)
    safety_curtain = IntelligentGrating(client, address=0x41)
    while True:
        #print(await io.read_channels())
        print(await sizing_curtain.read_blocked_quantity())
        print(await safety_curtain.read_blocked_quantity())


if __name__ == '__main__':
    asyncio.run(main())
