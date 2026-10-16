import pytest
from unittest.mock import AsyncMock, Mock
from hardware.controller import SharedModbusController


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
