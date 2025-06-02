from perpetual_waveshare.digital import Baudrate, DigitalIOController, Parity
from perpetual_waveshare.utils import get_modbus_port
import pymodbus.client as ModbusClient
from pymodbus.framer import FramerType
import asyncio


async def main() -> None:
    port = get_modbus_port()
    if port:
        client = ModbusClient.AsyncModbusSerialClient(
            port, framer=FramerType.RTU, baudrate=115200
        )
        await client.connect()
        io = DigitalIOController(client, address=1)
        result = await io.read_channels()
        for x in range(0, 10):
            for y in range(0, 8):
                await io.set_channel_on(y)
                await asyncio.sleep(.5)
            for z in range(0, 8):
                await io.set_channel_off(z)
                await asyncio.sleep(.5)
            for y in range(0, 8):
                await io.set_channel_on(y)
                await asyncio.sleep(.5)
        print(result)
        #await io.set_uart_paramters(Baudrate.B115200, Parity.NoParity)



if __name__ == '__main__':
    asyncio.run(main())
