from typing import List
import pymodbus.client as ModbusClient
from pymodbus.framer import FramerType
from pydantic import BaseModel

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


class ControlMode(IntEnum):
    Command = 0x0000
    Linked = 0x0001
    Flip = 0x0002


class Action(IntEnum):
    On = 0xFF00
    Off = 0x0000
    Flip = 0x5500


class Baudrate(IntEnum):
    B4800 = 0x00
    B9600 = 0x01
    B19200 = 0x02
    B38400 = 0x03
    B57600 = 0x04
    B115200 = 0x05
    B128000 = 0x06
    B256000 = 0x07


class Parity(IntEnum):
    NoParity = 0x00
    Even = 0x01
    Odd = 0x02


class IoBankState(BaseModel):
    ch0: bool = False
    ch1: bool = False
    ch2: bool = False
    ch3: bool = False
    ch4: bool = False
    ch5: bool = False
    ch6: bool = False
    ch7: bool = False

    @staticmethod
    def from_buffer(data: int) -> "IoBankState":
        return IoBankState(
            ch0=bool(data & 0x01),
            ch1=bool(data & 0x02),
            ch2=bool(data & 0x04),
            ch3=bool(data & 0x08),
            ch4=bool(data & 0x10),
            ch5=bool(data & 0x20),
            ch6=bool(data & 0x40),
            ch7=bool(data & 0x80),
        )


class DigitalIOController:
    def __init__(self, client: ModbusClient.AsyncModbusSerialClient, address: int = 1) -> None:
        self._client = client
        self._address = address

    def set_address(self, address: int) -> None:
        self._address = address

    async def connect(self) -> None:
        await self._client.connect()

    async def set_channel_on(self, channel: int) -> None:
        await self.set_channel(channel, Action.Off)

    async def set_channel_off(self, channel: int) -> None:
        await self.set_channel(channel, Action.On)

    async def set_channel(self, channel: int, action: Action) -> None:
        if channel > 7:
            raise Exception("Invalid Channel")
        await self._client.write_coil(
            OutputRegisterBases.OutputChannel + channel, action == Action.On, slave=self._address
        )

    async def set_channel_control_mode(self, channel: int, mode: ControlMode) -> None:
        if channel > 7:
            raise Exception("Invalid Channel")
        await self._client.write_registers(
            OutputRegisterBases.OutputChannel + channel, [mode], slave=self._address
        )

    async def set_channels(self, channel: int, actions: List[Action]) -> None:
        for ix, action in enumerate(actions):
            if channel + ix > 7:
                raise Exception("Invalid Channel")
            await self._client.write_coil(
                OutputRegisterBases.OutputChannel + channel + ix,
                action == Action.On,
                slave=self._address,
            )

    async def read_channels(self) -> IoBankState:
        response = await self._client.read_discrete_inputs(
            InputRegisterBases.InputChannels, count=8, slave=self._address
        )
        data = 0
        for ix, bit in enumerate(response.bits):
            if bit:
                data |= 0x01 << ix
        return IoBankState.from_buffer(data)

    async def read_output_channels(self) -> IoBankState:
        response = await self._client.read_coils(
            OutputRegisterBases.OutputChannel ,
            count=8,
            slave=self._address
        )
        data = 0
        for ix, bit in enumerate(response.bits):
            if bit:
                data |= 0x01 << ix
        return IoBankState.from_buffer(data)


    async def set_device_address(self, address: int) -> None:
        if address > 255:
            raise Exception("Invalid Address Requested")
        await self._client.write_register(
            HoldingRegisterBases.DeviceAddress, address, slave=self._address
        )

    async def set_uart_paramters(self, baudrate: Baudrate, parity: Parity) -> None:
        value = parity << 16 | baudrate
        await self._client.write_register(
            HoldingRegisterBases.UartParameters, value, slave=self._address
        )

    async def read_software_version(self) -> int:
        response = await self._client.read_holding_registers(
            HoldingRegisterBases.SoftwareVersion, count=1, slave=self._address
        )
        return response.registers[0]
