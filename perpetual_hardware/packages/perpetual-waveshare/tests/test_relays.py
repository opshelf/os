import asyncio
from perpetual_waveshare.utils import get_modbus_port
from perpetual_waveshare.relay import Action, RelayController


async def main() -> None:
    port = get_modbus_port()
    if port:
        rc = RelayController(port, 57600, 1)
        for i in range(0, 4):
            await rc.set_channel(i, Action.On)
            await asyncio.sleep(1)
        for i in range(0, 4):
            await rc.set_channel(i, Action.Off)
            await asyncio.sleep(1)


if __name__ == '__main__':
    asyncio.run(main())
