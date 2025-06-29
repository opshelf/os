from pymodbus.client.serial import AsyncModbusSerialClient
from typing import Optional, List
from enum import Enum
from pymodbus.framer.base import FramerType
from pymodbus.pdu import ModbusPDU

class Parity(Enum):
    NONE = 0
    ODD = 1
    EVEN = 2

class IntelligentGrating:
    def __init__(self, client: AsyncModbusSerialClient, address: int = 0x0F) -> None:
        self.client: AsyncModbusSerialClient = client
        self.address: int = address

    @classmethod
    async def build(cls, port: str, address: int = 0x0F, baudrate: int = 19200, 
                   parity: str = 'E', timeout: int = 1) -> 'IntelligentGrating':
        client: AsyncModbusSerialClient = AsyncModbusSerialClient(
            framer=FramerType.RTU,
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=1,
            bytesize=8,
            timeout=timeout
        )
        instance: IntelligentGrating = cls(client=client, address=address)
        await instance.connect()
        return instance

    async def connect(self) -> bool:
        return await self.client.connect()

    def close(self) -> None:
        self.client.close()


    async def set_address(self, new_address: int) -> None:
        await self._write_register(0xFFF0, new_address)

    async def set_baud_rate(self, baud: int) -> None:
        value: int = baud // 100
        await self._write_register(0xFFF1, value)

    async def set_parity(self, mode: Parity) -> None:
        await self._write_register(0xFFF2, mode.value)

    async def reset_defaults(self) -> ModbusPDU:
        return await self.client.write_register(0xFFF3,0x1111, slave=0x00)

    async def _write_register(self, reg: int, value: int, unit: Optional[int] = None) -> None:
        unit = unit or self.address
        await self.client.write_register(reg, value, slave=unit)

    async def _read_registers(self, start: int, count: int) -> ModbusPDU:
        return await self.client.read_holding_registers(start, count=count, slave=self.address)

    async def read_lowest_blocked(self) -> int:
        rr = await self._read_registers(0x0040, 1)
        return rr.registers[0] 

    async def read_highest_blocked(self) -> int:
        rr = await self._read_registers(0x0041, 1)
        return rr.registers[0] 

    async def read_blocked_quantity(self) -> int:
        rr = await self._read_registers(0x0042, 1)
        return rr.registers[0] 

    async def read_all_light_status(self, count_light_points: int = 80) -> List[int]:
        reg_count: int = (count_light_points + 15) // 16
        rr = await self._read_registers(0x0000, reg_count)
        bits = []
        for ix, num in enumerate(rr.registers):
            swapped = ((num & 0xFF00) >> 8) | ((num & 0x00FF) << 8)
            for idx in range(0, 16): 
                if idx + ix * 16 >= count_light_points:
                    pass 
                else:
                    if swapped & (0x01 << idx):
                        bits.append(1)
                    else:
                        bits.append(0)
        return bits

