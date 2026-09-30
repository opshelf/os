import asyncio
import logging
import pymodbus

from typing import Any, Awaitable, Callable, List, Optional, Sequence, TypeVar
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException, ModbusIOException

from .exceptions import (
    ModbusError, ModbusTimeoutError, ModbusConnectionError
)

logger = logging.getLogger(__name__).getChild("controller")

T = TypeVar("T")

TRANSIENT_MODBUS_ERRORS = (
    ModbusIOException,
    ModbusConnectionError,
    ModbusTimeoutError,
    ModbusException,
    asyncio.TimeoutError,
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
        self.lock = asyncio.Lock()

    def _require_connected(self) -> AsyncModbusSerialClient:
        if self.client is None or self.client.connected is not True:
            raise ModbusConnectionError("Not connected to Modbus device")

        return self.client

    async def _run_with_reconnect_retry(
        self,
        operation: Callable[[AsyncModbusSerialClient], Awaitable[T]],
        *,
        attempts: int,
        failure_message: str,
        unexpected_message: str,
        retry_modbus_errors: bool = False,
        retry_log_context: dict[str, Any] | None = None,
    ) -> T:
        for attempt in range(attempts):
            try:
                client = self._require_connected()
                return await operation(client)

            except TRANSIENT_MODBUS_ERRORS as e:
                if attempt < attempts - 1:
                    reconnected = await self._reconnect()

                    if reconnected is False:
                        raise ModbusConnectionError(f"Reconnection failed after error: {str(e)}") from e

                    continue

                raise ModbusError(f"{failure_message}: {str(e)}") from e

            except ModbusError as e:
                if retry_modbus_errors is True and attempt < attempts - 1:
                    logger.warning(
                        "Modbus operation failed, retrying",
                        extra={
                            **(retry_log_context or {}),
                            "attempt": attempt + 1,
                            "max_attempts": attempts,
                            "error": str(e),
                        },
                    )

                    await asyncio.sleep(0.05 * (attempt + 1))

                    continue

                raise

            except Exception as e:
                raise ModbusError(f"{unexpected_message}: {str(e)}") from e

        raise ModbusError(failure_message)

    def _validate_modbus_response(
        self,
        result: Any,
        *,
        no_response_message: str,
        error_message: str,
    ) -> Any:
        if result is None:
            raise ModbusError(no_response_message)

        if result.isError():
            raise ModbusError(f"{error_message}: {result}")

        return result

    def _validate_registers(
        self,
        result: Any,
        *,
        unit_id: int,
        function_code: int,
        address: int,
        count: int,
    ) -> tuple[int, ...]:
        registers = getattr(result, "registers", None)

        if not isinstance(registers, Sequence):
            raise ModbusError(
                f"Missing register payload from unit {unit_id}, function {function_code}, register {address}"
            )

        if len(registers) != count:
            raise ModbusError(
                f"Expected {count} register(s) from unit {unit_id}, function {function_code}, register "
                f"{address}, got {len(registers)}"
            )

        return tuple(int(value) for value in registers)

    async def _reconnect(self) -> bool:
        try:
            if self.client:
                await self.close()

            await self.connect()

            return self.client is not None and self.client.connected
        except Exception:
            return False

    async def connect(self) -> None:
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
                
        except ModbusConnectionError:
            raise
        except ModbusException as e:
            raise ModbusConnectionError(f"Modbus connection error: {str(e)}") from e
        except Exception as e:
            raise ModbusConnectionError(f"Unexpected error during connection: {str(e)}") from e

    async def close(self):
        if self.client is not None:
            self.client.close()

    async def read_register_block(
        self,
        unit_id: int,
        address: int,
        count: int = 1,
        function_code: int = 3,
        attempts: int = 3,
    ) -> tuple[int, ...]:
        """Read registers, retrying inside the port lock.

        `attempts` bounds that retry loop. Callers that are latency bound and have
        their own failure handling pass 1, because a retry here holds the port lock
        for another full device timeout.
        """
        self._require_connected()

        if count <= 0:
            raise ValueError("count must be greater than zero")

        if function_code not in (3, 4):
            raise ValueError("function_code must be 3 or 4")

        attempts = max(1, int(attempts))

        async def read_operation(client: AsyncModbusSerialClient) -> tuple[int, ...]:
            if function_code == 4:
                result = await client.read_input_registers(
                    address=address,
                    count=count,
                    device_id=unit_id
                )
            else:
                result = await client.read_holding_registers(
                    address=address,
                    count=count,
                    device_id=unit_id
                )

            result = self._validate_modbus_response(
                result,
                no_response_message=(
                    f"No response from unit {unit_id} while reading function "
                    f"{function_code} "
                    f"{count} register(s) starting at {address}"
                ),
                error_message=f"Read error from unit {unit_id}, function {function_code}, register {address}",
            )

            registers = self._validate_registers(
                result,
                unit_id=unit_id,
                function_code=function_code,
                address=address,
                count=count,
            )

            logger.debug(
                "Modbus register block read",
                extra={
                    "unit_id": unit_id,
                    "function_code": function_code,
                    "address": address,
                    "count": count,
                    "registers": [int(value) for value in registers],
                }
            )

            return registers

        async with self.lock:
            return await self._run_with_reconnect_retry(
                read_operation,
                attempts=attempts,
                failure_message="Modbus read failed after reconnect",
                unexpected_message=f"Unexpected error reading from unit {unit_id}",
                retry_modbus_errors=True,
                retry_log_context={
                    "unit_id": unit_id,
                    "function_code": function_code,
                    "address": address,
                    "count": count,
                },
            )

    async def read_holding_register_block(
        self,
        unit_id: int,
        address: int,
        count: int = 1,
        attempts: int = 3,
    ) -> tuple[int, ...]:
        return await self.read_register_block(
            unit_id=unit_id,
            address=address,
            count=count,
            function_code=3,
            attempts=attempts,
        )

    async def read_input_register_block(
        self,
        unit_id: int,
        address: int,
        count: int = 1,
    ) -> tuple[int, ...]:
        return await self.read_register_block(
            unit_id=unit_id,
            address=address,
            count=count,
            function_code=4,
        )

    async def read_holding_registers(
        self,
        unit_id: int,
        address: int,
        count: int = 1,
    ) -> int:
        registers = await self.read_holding_register_block(
            unit_id=unit_id,
            address=address,
            count=count,
        )

        return int(registers[0])

    async def write_single_register(
        self,
        unit_id: int,
        address: int,
        value: int
    ) -> None:
        self._require_connected()

        async def write_operation(client: AsyncModbusSerialClient) -> None:
            result = await client.write_register(
                address=address,
                value=value,
                device_id=int(unit_id)
            )

            self._validate_modbus_response(
                result,
                no_response_message=(
                    f"No response from unit {unit_id} while writing "
                    f"value {value} to register {address}"
                ),
                error_message=f"Write error to unit {unit_id}, register {address}",
            )

        async with self.lock:
            await self._run_with_reconnect_retry(
                write_operation,
                attempts=2,
                failure_message="Modbus write failed after reconnect",
                unexpected_message=f"Unexpected error writing to unit {unit_id}",
            )

    async def write_multiple_registers(
        self,
        unit_id: int,
        address: int,
        values: List[int]
    ) -> None:
        self._require_connected()

        if not values:
            raise ValueError("Values list cannot be empty")

        async def write_operation(client: AsyncModbusSerialClient) -> None:
            result = await client.write_registers(
                address=address,
                values=values,
                device_id=int(unit_id)
            )

            self._validate_modbus_response(
                result,
                no_response_message=(
                    f"No response from unit {unit_id} while writing "
                    f"{len(values)} values starting at register {address}"
                ),
                error_message=f"Write error to unit {unit_id}, registers {address}-{address+len(values)-1}",
            )

        async with self.lock:
            await self._run_with_reconnect_retry(
                write_operation,
                attempts=2,
                failure_message="Modbus multi-write failed after reconnect",
                unexpected_message=f"Unexpected error writing to unit {unit_id}",
            )
