import asyncio
import logging
import pymodbus

from typing import Any, Awaitable, Callable, Optional, Sequence, TypeVar
from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException, ModbusIOException

from .exceptions import (
    ModbusError, ModbusTimeoutError, ModbusConnectionError,
    ModbusTransientNoResponseError
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

# pymodbus gives up on a request the drive never answered by exhausting its own retries and
# raising `ModbusIOException` carrying exactly this text (3.11, transaction/transaction.py);
# `ModbusException.__str__` prefixes it with "Modbus Error: ", and pymodbus' Log.error also
# appends the last frames as ">>>>> send: ..."/">>>>> recv: ..." lines when DEBUG logging is
# off -- that trailer reaches the log record, and is stripped below in case a future pymodbus
# raises with it too. Keep these two strings in step with the same constants in
# openshelf_os.__init__, which is what decides whether a report reaches Sentry.
TRANSIENT_NO_RESPONSE_TEXT = "No response received after "
TRANSIENT_NO_RESPONSE_SUFFIX = " retries, continue with next request"
PYMODBUS_FRAME_DUMP_LINE_PREFIX = ">>>>> "


def strip_pymodbus_frame_dump(message: str) -> str:
    """Drop the frame dump pymodbus appends to its messages, if one is there.

    Only the first line carries the log text; every appended line starts with the marker
    prefix, and a deduplicated frame shows up as ">>>>> Repeating....". Anything else after
    a newline is left untouched.
    """
    log_text, separator, trailer = message.partition("\n")
    if separator == "":
        return message

    trailer_lines = [line for line in trailer.split("\n") if line.strip() != ""]
    if trailer_lines == []:
        return message

    if any(line.startswith(PYMODBUS_FRAME_DUMP_LINE_PREFIX) is False for line in trailer_lines):
        return message

    return log_text


def no_response_retry_count(message: Any) -> int | None:
    """The retry count of pymodbus' "gave up, no reply" text, or None for any other message."""
    if not isinstance(message, str):
        return None

    log_text = strip_pymodbus_frame_dump(message)

    start = log_text.find(TRANSIENT_NO_RESPONSE_TEXT)
    if start == -1 or log_text.endswith(TRANSIENT_NO_RESPONSE_SUFFIX) is False:
        return None

    count = log_text[start + len(TRANSIENT_NO_RESPONSE_TEXT) : -len(TRANSIENT_NO_RESPONSE_SUFFIX)]
    return int(count) if count.isdigit() else None


def is_no_response_error(exc: BaseException | None) -> bool:
    """Whether an error is pymodbus' own "the drive never answered", or wraps one unchanged.

    A drive that stayed silent through every retry is a dropout on a bus that still reconnects,
    not a device fault: the request went out and nothing came back. A Modbus error the drive
    answered badly, or a port that will not reopen, is a different failure and says so.

    Walks the cause chain so the wrapper that carries the failure onward is enough -- every
    error the hardware layer raises for one of these keeps this one as its cause.
    """
    seen: set[int] = set()
    current: BaseException | None = exc

    while current is not None and id(current) not in seen:
        seen.add(id(current))

        if getattr(current, "transient", False) is True:
            return True

        if isinstance(current, ModbusIOException) is True:
            if no_response_retry_count(str(current)) is not None:
                return True

        current = current.__cause__ or current.__context__

    return False


def _no_response_message(exc: BaseException) -> str:
    """The pymodbus text of a no-response error, without a caller's wrapper around it.

    The bare text is what the openshelf_os Sentry hooks recognise, so it is kept byte for byte:
    pymodbus prefixes its wire errors with "[Input/Output] " and `ModbusException.__str__` adds
    "Modbus Error: ", and neither is part of the pattern the filter matches.
    """
    retry_count = no_response_retry_count(str(exc))
    if retry_count is None:
        return strip_pymodbus_frame_dump(str(exc))

    return f"{TRANSIENT_NO_RESPONSE_TEXT}{retry_count}{TRANSIENT_NO_RESPONSE_SUFFIX}"


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
        retry_on_transient_errors: bool = False,
    ) -> T:
        for attempt in range(attempts):
            try:
                client = self._require_connected()
                return await operation(client)

            except TRANSIENT_MODBUS_ERRORS as e:
                # A drive that never answered is retried whether or not the caller opted in.
                # Any other transient error may have killed the port, so it is retried into a
                # freshly reopened one only for callers that accept that; a write in particular
                # may be what made the drive stop answering (a control word that put it into a
                # save, say), and on a bus with several drives behind one port the next attempt
                # must not leave stale bytes for whoever reads next.
                silent_drive = is_no_response_error(e)
                retryable = silent_drive or retry_on_transient_errors is True
                # A missed round of replies on a live bus is the dropout pymodbus expects on a
                # shared RTU line and already logs at ERROR, so it is not worth a WARNING of our
                # own on every retry; anything else that needs one gets it.
                log_retry_at_debug = silent_drive or isinstance(e, ModbusTimeoutError)

                if attempt < attempts - 1:
                    if retryable is False:
                        raise ModbusConnectionError(f"Port unusable after error: {str(e)}") from e

                    reconnected = await self._reconnect()

                    if reconnected is False:
                        raise ModbusConnectionError(f"Reconnection failed after error: {str(e)}") from e

                    logger.log(
                        logging.DEBUG if log_retry_at_debug is True else logging.WARNING,
                        "Transient Modbus error, retrying",
                        extra={
                            **(retry_log_context or {}),
                            "attempt": attempt + 1,
                            "max_attempts": attempts,
                            "port": self.port,
                            "reconnected": reconnected,
                            "error": str(e),
                        },
                    )

                    continue

                if silent_drive is True:
                    # The drive stayed silent through pymodbus' own retries and through ours on a
                    # port that reconnects, so this is the dropout pymodbus itself expects on a
                    # shared RTU line and already logs. Raised with pymodbus' own text, the report
                    # is one the Sentry hooks in openshelf_os can drop (they match on that text,
                    # not on a caller's wording) whatever type ends up carrying it; the type here
                    # is what a log or a crash context will name it as.
                    raise ModbusTransientNoResponseError(_no_response_message(e)) from e

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
                # Close is synchronous on the pymodbus client; calling it through this class'
                # `async def close` would only hand the coroutine to the loop unwaited.
                self.client.close()

            # A reconnect is a fresh port and a fresh transport, so the per-call retry and
            # timeout overrides a caller applied to the previous client do not survive it --
            # the next attempt runs on the client defaults.
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
        modbus_retries: Optional[int] = None,
        modbus_timeout_s: Optional[float] = None,
    ) -> tuple[int, ...]:
        """Read registers, retrying inside the port lock.

        `attempts` bounds that retry loop. Callers that are latency bound and have
        their own failure handling pass 1, because a retry here holds the port lock
        for another full device timeout.

        `modbus_retries` and `modbus_timeout_s` override the client's own retry
        count and per-attempt timeout for the duration of this read. The guard's
        fast weigh uses `modbus_retries=0` and a short timeout so one read can
        never hold the port lock for the client default (3 retries at `timeout`).
        A caller that passes them gets one attempt and no retry loop: a reconnect
        inside them would build a fresh client at the client defaults and spend
        the full retry budget the override exists to prevent.
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
            if modbus_retries is None and modbus_timeout_s is None:
                return await self._run_with_reconnect_retry(
                    read_operation,
                    attempts=attempts,
                    failure_message="Modbus read failed after reconnect",
                    unexpected_message=f"Unexpected error reading from unit {unit_id}",
                    retry_modbus_errors=True,
                    retry_on_transient_errors=True,
                    retry_log_context={
                        "unit_id": unit_id,
                        "function_code": function_code,
                        "address": address,
                        "count": count,
                    },
                )

            client = self._require_connected()
            previous_retries = client.ctx.retries
            previous_timeout = client.comm_params.timeout_connect
            try:
                if modbus_retries is not None:
                    client.ctx.retries = modbus_retries
                if modbus_timeout_s is not None:
                    client.comm_params.timeout_connect = modbus_timeout_s
                # The latency-bound caller asked for one attempt precisely so this call cannot
                # hold the port lock for another device timeout, so it keeps its single attempt
                # and its own failure handling.
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
            finally:
                client.ctx.retries = previous_retries
                client.comm_params.timeout_connect = previous_timeout

    async def read_holding_register_block(
        self,
        unit_id: int,
        address: int,
        count: int = 1,
        attempts: int = 3,
        modbus_retries: Optional[int] = None,
        modbus_timeout_s: Optional[float] = None,
    ) -> tuple[int, ...]:
        return await self.read_register_block(
            unit_id=unit_id,
            address=address,
            count=count,
            function_code=3,
            attempts=attempts,
            modbus_retries=modbus_retries,
            modbus_timeout_s=modbus_timeout_s,
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
                retry_log_context={"unit_id": unit_id, "address": address, "value": value},
                retry_on_transient_errors=True,
            )
