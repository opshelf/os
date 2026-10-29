import pytest
from unittest.mock import AsyncMock, Mock
from pymodbus.exceptions import ConnectionException, ModbusIOException

from hardware.controller import (
    ModbusTransientNoResponseError,
    SharedModbusController,
    is_no_response_error,
)

# pymodbus' own text for a request the drive never answered, as the exception reaches us it
# (WIRE_..., plus the "Modbus Error: [Input/Output] " ModbusException.__str__ puts in front of
# it), and NO_RESPONSE_WITH_FRAMES with the frame dump pymodbus puts on its log records -- the
# shape the controller has to tolerate if a later pymodbus raises with it too.
PYMODBUS_NO_RESPONSE = "No response received after 3 retries, continue with next request"
WIRE_NO_RESPONSE = f"[Input/Output] {PYMODBUS_NO_RESPONSE}"
NO_RESPONSE_WITH_FRAMES = f"{WIRE_NO_RESPONSE}\n>>>>> send: 0106\n>>>>> recv: "


def _dropped_write_controller(side_effect, *, reconnected=True):
    client = AsyncMock()
    client.connected = True
    client.write_register = AsyncMock(side_effect=side_effect)

    controller = SharedModbusController("/dev/ttyUSB0", 9600)
    controller.client = client
    # The client the tests hand out is a mock, so the reconnect that separates the two
    # attempts is stubbed: the client stays "connected" across it, as it does on a port
    # that really does reopen.
    controller._reconnect = AsyncMock(return_value=reconnected)
    return controller, client


@pytest.mark.asyncio
async def test_read_overrides_modbus_retries_and_timeout_for_the_duration():
    client = AsyncMock()
    client.connected = True
    client.ctx = Mock()
    client.ctx.retries = 3
    client.comm_params = Mock()
    client.comm_params.timeout_connect = 2.0

    ok_result = Mock()
    ok_result.isError.return_value = False
    ok_result.registers = [42]
    observed: dict[str, object] = {}

    async def read_holding_registers(*_args, **_kwargs):
        observed["retries"] = client.ctx.retries
        observed["timeout"] = client.comm_params.timeout_connect
        return ok_result

    client.read_holding_registers = AsyncMock(side_effect=read_holding_registers)

    controller = SharedModbusController("/dev/ttyUSB0", 9600)
    controller.client = client

    await controller.read_register_block(
        1, 0, 1, function_code=3, attempts=1, modbus_retries=0, modbus_timeout_s=0.1
    )

    assert observed == {"retries": 0, "timeout": 0.1}
    assert client.ctx.retries == 3
    assert client.comm_params.timeout_connect == 2.0


@pytest.mark.asyncio
async def test_write_retry_after_a_drive_that_never_answered_is_transient():
    # The request went out twice and the drive never replied, so this is a dropout on a bus
    # that still reconnects. It has to surface as pymodbus' own no-response text: that is the
    # message the openshelf_os Sentry hooks drop, while a wrapper around it ("Modbus write
    # failed after reconnect: ...") reached Sentry as a motor error on every dropout.
    controller, client = _dropped_write_controller(ModbusIOException(NO_RESPONSE_WITH_FRAMES))

    with pytest.raises(ModbusTransientNoResponseError) as exc_info:
        await controller.write_single_register(2, 0x6000, 0x1111)

    # The report drops pymodbus' prefixes along with the caller's wrapper, because the Sentry
    # filters match on pymodbus' own text and on nothing around it.
    assert str(exc_info.value) == PYMODBUS_NO_RESPONSE
    assert is_no_response_error(exc_info.value) is True
    assert client.write_register.await_count == 2


@pytest.mark.asyncio
async def test_write_recovers_when_the_drive_answers_the_retry():
    controller, client = _dropped_write_controller(
        [ModbusIOException(WIRE_NO_RESPONSE), Mock(isError=Mock(return_value=False))]
    )

    await controller.write_single_register(2, 0x6000, 0x1111)

    assert client.write_register.await_count == 2


@pytest.mark.asyncio
async def test_write_reconnect_failure_still_fails_fast():
    # A port that cannot be reopened after an error that is not a silent drive (here a device
    # that answered badly) is a connection problem: the reconnect helper's error, after a
    # single attempt rather than a second write into a dead port.
    controller, client = _dropped_write_controller(
        ConnectionException("port went away"), reconnected=False
    )

    with pytest.raises(Exception) as exc_info:
        await controller.write_single_register(2, 0x6000, 0x1111)

    assert "Reconnection failed after error" in str(exc_info.value)
    assert client.write_register.await_count == 1


@pytest.mark.asyncio
async def test_write_real_modbus_failure_keeps_context_after_reconnect():
    # An error that is not a silent drive (an exception response, a bad payload) is a genuine
    # failure: it stays wrapped so the operator-facing error and the crash report still say
    # what was being written, and it stays reportable.
    # What a drive that answered badly looks like: pymodbus' own prefix, without its text.
    controller, _client = _dropped_write_controller(
        ModbusIOException("Write error to unit 2")
    )

    with pytest.raises(Exception) as exc_info:
        await controller.write_single_register(2, 0x6000, 0x1111)

    assert type(exc_info.value).__name__ == "ModbusError"
    assert str(exc_info.value) == (
        "Modbus write failed after reconnect: "
        "Modbus Error: [Input/Output] Write error to unit 2"
    )
    assert is_no_response_error(exc_info.value) is False


@pytest.mark.asyncio
async def test_read_retries_a_silent_drive_without_failing_the_caller():
    # Position and status are read on every motion poll, so a drive that misses one round of
    # replies must be retried by the read path too -- otherwise a single dropout surfaces as a
    # failed move and takes the axis down.
    client = AsyncMock()
    client.connected = True
    client.ctx = Mock()
    client.ctx.retries = 3
    client.comm_params = Mock()
    client.comm_params.timeout_connect = 2.0
    ok = Mock()
    ok.isError.return_value = False
    ok.registers = [7]
    client.read_holding_registers = AsyncMock(
        side_effect=[ModbusIOException(WIRE_NO_RESPONSE), ok]
    )

    controller = SharedModbusController("/dev/ttyUSB0", 9600)
    controller.client = client
    controller._reconnect = AsyncMock(return_value=True)

    registers = await controller.read_holding_register_block(2, 0x1003, 1)

    assert registers == (7,)
    assert client.read_holding_registers.await_count == 2
