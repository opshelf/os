from perpetual_os1.utils import get_modbus_client
from perpetual_waveshare.digital import DigitalIOController
import asyncio

async def main() -> None:
    client = await get_modbus_client()
    io = DigitalIOController(client)
    await io.set_channel_on(4)
    await asyncio.sleep(5)
    await io.set_channel_off(4)
    await asyncio.sleep(5)
    result = await io.read_channels()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
