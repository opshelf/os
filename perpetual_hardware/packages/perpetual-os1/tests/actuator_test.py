from perpetual_os1.utils import get_modbus_client
from perpetual_waveshare.digital import DigitalIOController
import asyncio

async def main() -> None:
    client = await get_modbus_client()
    io = DigitalIOController(client)
    result = await io.read_channels()
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
