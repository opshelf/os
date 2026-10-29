import asyncio
import time

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


@pytest.mark.asyncio
async def test_read_queued_behind_a_busy_write_waits_out_the_busy_window():
    # Both callers pass the pre-lock check while unit 1 is idle; the busy write then runs
    # first, so the queued read must not go out inside the window it opened.
    busy_s = 0.2
    sent: dict[str, float] = {}
    other_unit_started = asyncio.Event()
    release_other_unit = asyncio.Event()

    ok_result = Mock()
    ok_result.isError.return_value = False
    ok_result.registers = [1]

    async def write_register(*, address, value, device_id):
        if device_id == 2:
            other_unit_started.set()
            await release_other_unit.wait()
        else:
            sent["reset"] = time.monotonic()
        return ok_result

    async def read_holding_registers(**_kwargs):
        sent["read"] = time.monotonic()
        return ok_result

    client = AsyncMock()
    client.connected = True
    client.write_register = write_register
    client.read_holding_registers = read_holding_registers

    controller = SharedModbusController("/dev/ttyUSB0", 9600)
    controller.client = client

    other = asyncio.create_task(controller.write_single_register(2, 0x6000, 0))
    await other_unit_started.wait()
    reset = asyncio.create_task(
        controller.write_single_register(1, 0x1122, 0x1122, busy_after_s=busy_s)
    )
    await asyncio.sleep(0)
    read = asyncio.create_task(controller.read_register_block(1, 0x6002, 1, attempts=1))
    await asyncio.sleep(0)
    release_other_unit.set()
    await asyncio.gather(other, reset, read)

    assert sent["read"] - sent["reset"] >= busy_s
