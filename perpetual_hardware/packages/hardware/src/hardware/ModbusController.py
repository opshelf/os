import pymodbus
import asyncio
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusIOException
from typing import Optional

class SharedModbusController:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.client: Optional[AsyncModbusSerialClient] = None
        self.lock = asyncio.Lock()  # ensures only one coroutine accesses the bus at a time

    async def connect(self):
        self.client = AsyncModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            retries=1,
            framer=pymodbus.FramerType.RTU
        )
        await self.client.connect()
        if not self.client.connected:
            raise ConnectionError(f"Failed to connect to Modbus device on port {self.port}")

    async def close(self):
        if self.client:
            await self.client.close()

    async def read_holding_registers(self, unit_id: int, address: int) -> int:
        async with self.lock:
            try:
                result = await self.client.read_holding_registers(address=address, count=1, slave=unit_id)
                if result.isError():
                    raise ModbusIOException(f"Read error from unit {unit_id}: {result}")
                return result.registers
            except Exception as e:
                print(f"[READ ERROR] {e}")
                return None

    async def write_single_register(self, unit_id: int, address: int, value: int):
        async with self.lock:
            try:
                result = await self.client.write_register(address=address, value=value, slave=int(unit_id))
                if result.isError():
                    raise ModbusIOException(f"Write error to unit {unit_id}: {result}")
                return result
            except Exception as e:
                print(f"[WRITE ERROR] {e}")
                return None

