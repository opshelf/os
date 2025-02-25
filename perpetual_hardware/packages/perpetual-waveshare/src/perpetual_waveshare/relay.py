from typing import List, Self
import pymodbus.client as ModbusClient
from pymodbus.framer import FramerType
from pydantic import BaseModel
#from typing import Optional

from enum import IntEnum


class OutputRegisterBases(IntEnum):
    OutputChannel = 0x0000
    ControlAllRegisters = 0x00FF
    ToggleRelays = 0x01FF
    OutputChannelFlashOpen = 0x0200
    OutputChannelFlashClose = 0x0400


class InputRegisterBases(IntEnum):
    InputChannels = 0x0000


class HoldingRegisterBases(IntEnum):
    ControlMode = 0x1000
    UartParameters = 0x2000
    DeviceAddress = 0x4000
    SoftwareVersion = 0x8000


class Action(IntEnum):
    On = 0xFF00
    Off = 0x0000
    Flip = 0x5500


class RelayState(BaseModel):
    relay_0: bool = False
    relay_1: bool = False
    relay_2: bool = False
    relay_3: bool = False
    relay_4: bool = False
    relay_5: bool = False
    relay_6: bool = False
    relay_7: bool = False

    @staticmethod
    def from_buffer(data: int) -> "RelayState":
        return RelayState(
            relay_0=bool(data & 0x01),
            relay_1=bool(data & 0x02),
            relay_2=bool(data & 0x04),
            relay_3=bool(data & 0x08),
            relay_4=bool(data & 0x10),
            relay_5=bool(data & 0x20),
            relay_6=bool(data & 0x40),
            relay_7=bool(data & 0x80)
        )


class RelayController:

    def __init__(self, port: str, baudrate: int, address: int = 1) -> None:
        self._client = ModbusClient.AsyncModbusSerialClient(port, framer=FramerType.RTU, baudrate=baudrate) 
        self._address = address

    def set_address(self, address: int) -> None:
        self._address = address

    async def connect(self) -> None:
        await self._client.connect()

    async def open_channel(self, channel: int) -> None:
        await self.set_channel(channel, Action.Off)

    async def close_channel(self, channel: int) -> None:
        await self.set_channel(channel, Action.On)

    async def set_channel(self, channel: int, action: Action) -> None: 
        if channel > 7:
            raise Exception("Invalid Channel")
        await self._client.write_coil(OutputRegisterBases.OutputChannel + channel, action == Action.On, slave=self._address)

    async def set_channels(self, channel: int, actions: List[Action]) -> None: 
        for ix, action in enumerate(actions):
            if channel + ix > 7:
                raise Exception("Invalid Channel")
            await self._client.write_coil(OutputRegisterBases.OutputChannel + channel + ix, action == Action.On, slave=self._address)

    async def read_channels(self) -> RelayState: 
        response = await self._client.read_coils(InputRegisterBases.InputChannels, count=8, slave=self._address)
        data = 0
        for ix, bit in enumerate(response.bits):
            if bit: 
                data |= 0x01 << ix
        return RelayState.from_buffer(data)
