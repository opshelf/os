import pymodbus
import asyncio
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusIOException, ModbusException
from typing import Optional, Any, Union, List

from .exceptions import (
    ModbusError, ModbusTimeoutError, ModbusConnectionError,
    CommunicationError
)

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
        

    async def _reconnect(self) -> bool:
        """Attempt to reconnect to the Modbus device. Returns True if successful."""
        try:
            if self.client:
                await self.close()
            await self.connect()
            return self.client is not None and self.client.connected
        except Exception:
            return False

    async def connect(self) -> None:
        """
        Initialize and connect to the Modbus device.
        
        Raises:
            ModbusConnectionError: If connection fails
            ModbusConfigError: If client configuration is invalid
        """
        try:
            self.client = AsyncModbusSerialClient(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                parity=self.parity,
                bytesize=8,
                retries=3,
                stopbits=1,
                framer=pymodbus.FramerType.RTU
            )
            
            connected = await self.client.connect()
            if not connected or not self.client.connected:
                raise ModbusConnectionError(
                    f"Failed to connect to Modbus device on port {self.port} "
                    f"(baudrate={self.baudrate}, parity={self.parity})"
                )
                
        except ModbusException as e:
            raise ModbusConnectionError(f"Modbus connection error: {str(e)}") from e
        except Exception as e:
            raise ModbusConnectionError(f"Unexpected error during connection: {str(e)}") from e

    async def close(self):
        if self.client is not None:
            self.client.close()

    async def read_holding_registers(
        self, 
        unit_id: int, 
        address: int, 
        count: int = 1
    ) -> int:
        """
        Read value(s) from holding registers.
        
        Args:
            unit_id: Modbus device ID
            address: Register address to read from
            count: Number of registers to read
            
        Raises:
            ModbusError: If read operation fails
            ModbusTimeoutError: If operation times out
            ModbusConnectionError: If connection is lost
            
        Returns:
            Integer value read from register
        """
        if not self.client or not self.client.connected:
            raise ModbusConnectionError("Not connected to Modbus device")

        async with self.lock:
            for attempt in range(2):
                try:
                    result = await self.client.read_holding_registers(
                        address=address,
                        count=count,
                        device_id=unit_id
                    )
                    if result is None:
                        raise ModbusError(
                            f"No response from unit {unit_id} while reading "
                            f"register {address}"
                        )
                    if result.isError():
                        raise ModbusError(
                            f"Read error from unit {unit_id}, register {address}: {result}"
                        )
                    return int(result.registers[0])
                except (ModbusIOException, ModbusConnectionError, ModbusTimeoutError, ModbusException, asyncio.TimeoutError) as e:
                    if attempt == 0:
                        reconnected = await self._reconnect()
                        if not reconnected:
                            raise ModbusConnectionError(f"Reconnection failed after error: {str(e)}") from e
                        continue
                    raise ModbusError(f"Modbus read failed after reconnect: {str(e)}") from e
                except Exception as e:
                    raise ModbusError(f"Unexpected error reading from unit {unit_id}: {str(e)}") from e

    async def write_single_register(
        self,
        unit_id: int,
        address: int,
        value: int
    ) -> None:
        """
        Write a value to a single register.
        
        Args:
            unit_id: Modbus device ID
            address: Register address to write to
            value: Value to write
            
        Raises:
            ModbusError: If write operation fails
            ModbusTimeoutError: If operation times out
            ModbusConnectionError: If connection is lost
        """
        if not self.client or not self.client.connected:
            raise ModbusConnectionError("Not connected to Modbus device")

        async with self.lock:
            for attempt in range(2):
                try:
                    result = await self.client.write_register(
                        address=address,
                        value=value,
                        device_id=int(unit_id)
                    )
                    if result is None:
                        raise ModbusError(
                            f"No response from unit {unit_id} while writing "
                            f"value {value} to register {address}"
                        )
                    if result.isError():
                        raise ModbusError(
                            f"Write error to unit {unit_id}, register {address}: {result}"
                        )
                    return
                except (ModbusIOException, ModbusConnectionError, ModbusTimeoutError, ModbusException, asyncio.TimeoutError) as e:
                    if attempt == 0:
                        reconnected = await self._reconnect()
                        if not reconnected:
                            raise ModbusConnectionError(f"Reconnection failed after error: {str(e)}") from e
                        continue
                    raise ModbusError(f"Modbus write failed after reconnect: {str(e)}") from e
                except Exception as e:
                    raise ModbusError(f"Unexpected error writing to unit {unit_id}: {str(e)}") from e

    async def write_multiple_registers(
        self,
        unit_id: int,
        address: int,
        values: List[int]
    ) -> None:
        """
        Write values to multiple consecutive registers.
        
        Args:
            unit_id: Modbus device ID
            address: Starting register address 
            values: List of values to write
            
        Raises:
            ModbusError: If write operation fails
            ModbusTimeoutError: If operation times out
            ModbusConnectionError: If connection is lost
            ValueError: If values list is empty
        """
        if not self.client or not self.client.connected:
            raise ModbusConnectionError("Not connected to Modbus device")

        if not values:
            raise ValueError("Values list cannot be empty")

        async with self.lock:
            for attempt in range(2):
                try:
                    result = await self.client.write_registers(
                        address=address,
                        values=values,
                        device_id=int(unit_id)
                    )
                    if result is None:
                        raise ModbusError(
                            f"No response from unit {unit_id} while writing "
                            f"{len(values)} values starting at register {address}"
                        )
                    if result.isError():
                        raise ModbusError(
                            f"Write error to unit {unit_id}, registers {address}-{address+len(values)-1}: {result}"
                        )
                    return
                except (ModbusIOException, ModbusConnectionError, ModbusTimeoutError, ModbusException, asyncio.TimeoutError) as e:
                    if attempt == 0:
                        reconnected = await self._reconnect()
                        if not reconnected:
                            raise ModbusConnectionError(f"Reconnection failed after error: {str(e)}") from e
                        continue
                    raise ModbusError(f"Modbus multi-write failed after reconnect: {str(e)}") from e
                except Exception as e:
                    raise ModbusError(f"Unexpected error writing to unit {unit_id}: {str(e)}") from e

