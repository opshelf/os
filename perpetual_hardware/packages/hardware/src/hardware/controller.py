import pymodbus
import asyncio
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusIOException
from typing import Optional

class SharedModbusController:
    port: str
    baudrate: int
    timeout: float
    parity: str
    client: Optional[AsyncModbusSerialClient]
    lock: asyncio.Lock

    def __init__(self, port: str, baudrate: int, parity: str = "N", timeout: float = 2.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.parity = parity
        self.client = None
        self.lock = asyncio.Lock()  # ensures only one coroutine accesses the bus at a time
        

    async def connect(self):
        self.client = AsyncModbusSerialClient(
            port=self.port,
            baudrate=self.baudrate,
            timeout=self.timeout,
            parity="N",
            bytesize=8,
            retries=1,
            stopbits=1,
            framer=pymodbus.FramerType.RTU
        )
        await self.client.connect()
        if not self.client.connected:
            raise ConnectionError(f"Failed to connect to Modbus device on port {self.port}")

    async def close(self):
        if self.client is not None:
            self.client.close()

    async def read_holding_registers(self, unit_id: int, address: int, count: int = 1) -> Optional[int]:
        async with self.lock:
            try:
                result = await self.client.read_holding_registers(address=address, count=count, device_id=unit_id)
                if result.isError():
                    raise ModbusIOException(f"Read error from unit {unit_id}: {result}")
                return int(result.registers[0])
            except Exception as e:
                print(f"[READ ERROR] {e}")
                return None

    async def write_single_register(self, unit_id: int, address: int, value: int) -> Optional[bool]:
        async with self.lock:
            try:
                result = await self.client.write_register(address=address, value=value, device_id=int(unit_id))
                if result.isError():
                    raise ModbusIOException(f"Write error to unit {unit_id}: {result}")
                return True
            except Exception as e:
                print(f"[WRITE ERROR] {e}")
                return None

