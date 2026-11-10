import asyncio
import os
import pty
import time

import pytest
from unittest.mock import AsyncMock, Mock
from hardware.controller import SharedModbusController
from hardware.exceptions import ModbusError


@pytest.fixture
def silent_serial_port():
    """A serial port nothing answers on, driven by the real pymodbus client."""
    master, slave = pty.openpty()
    try:
        yield os.ttyname(slave)
    finally:
        os.close(master)
        os.close(slave)


async def test_bounded_read_fails_inside_its_own_timeout(silent_serial_port):
    # The motion guard's fast weigh and the bounded position reads hold the shared port lock.
    # Their timeout was written to an object pymodbus does not read, so a device that did not
    # answer held the bus for the full 2 s default instead of the 0.1 s asked for.
    controller = SharedModbusController(silent_serial_port, 9600, timeout=2.0)
    await controller.connect()

    started = time.monotonic()
    with pytest.raises(ModbusError):
        await controller.read_register_block(
            1, 0, 1, attempts=1, modbus_retries=0, modbus_timeout_s=0.1
        )
    elapsed = time.monotonic() - started
    restored = (controller.client.ctx.retries, controller.client.ctx.comm_params.timeout_connect)
    await controller.close()

    assert elapsed < 1.0
    assert restored == (3, 2.0)


async def test_a_cancelled_read_is_cancelled_instead_of_retried_over_a_reconnect(silent_serial_port):
    # pymodbus turns a task cancelled mid-request into an I/O error. The controller took that
    # for a transient fault: it reconnected the shared bus and sent the request again, so an
    # aborted caller kept running for seconds and a cancelled write could go out after all.
    controller = SharedModbusController(silent_serial_port, 9600, timeout=2.0)
    await controller.connect()
    controller._reconnect = AsyncMock(return_value=True)

    task = asyncio.create_task(controller.read_holding_registers(1, 0))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started
    await controller.close()

    assert elapsed < 0.5
    controller._reconnect.assert_not_awaited()


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
