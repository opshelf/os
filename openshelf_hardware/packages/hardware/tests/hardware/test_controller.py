import asyncio
import os
import pty
import time

import pytest
from pymodbus.framer import FramerRTU
from unittest.mock import AsyncMock, Mock
from hardware.controller import SharedModbusController
from hardware.exceptions import ModbusError


@pytest.fixture
def serial_drive():
    """A serial port for the real pymodbus client plus the drive's end of it (a pty master)."""
    master, slave = pty.openpty()
    try:
        yield os.ttyname(slave), master
    finally:
        os.close(master)
        os.close(slave)


@pytest.fixture
def silent_serial_port(serial_drive):
    """A serial port nothing answers on, driven by the real pymodbus client."""
    return serial_drive[0]


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


@pytest.mark.parametrize("late_reply", [True, False])
async def test_next_frame_after_a_cancelled_request_waits_for_the_stale_reply(serial_drive, late_reply):
    # A cancelled read leaves the drive's reply outstanding and RTU frames carry no transaction
    # id, so pymodbus resolves the next request to that unit with the stale reply: the stop a
    # cancelled move sends would be acknowledged before the drive had answered it. The hold
    # ends when that reply lands, so the stop is not delayed by the whole response timeout; a
    # drive that never answers is waited out for that long.
    port, drive = serial_drive
    os.set_blocking(drive, False)
    frames: list[tuple[float, bytes]] = []
    asyncio.get_running_loop().add_reader(drive, lambda: frames.append((time.monotonic(), os.read(drive, 256))))

    def rtu(payload: bytes) -> bytes:
        return payload + FramerRTU.compute_CRC(payload).to_bytes(2, "big")

    timeout = 0.5
    controller = SharedModbusController(port, 9600, timeout=timeout)
    await controller.connect()

    read = asyncio.create_task(controller.read_holding_registers(1, 0))
    await asyncio.sleep(0.1)
    assert len(frames) == 1
    started = time.monotonic()
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert time.monotonic() - started < 0.2

    stop_frame = rtu(bytes([1, 6, 0x60, 0x02, 0, 0x40]))
    stop = asyncio.create_task(controller.write_single_register(1, 0x6002, 0x40))
    await asyncio.sleep(0.1)
    assert len(frames) == 1
    if late_reply:
        os.write(drive, rtu(bytes([1, 3, 2, 0x12, 0x34])))
        await asyncio.sleep(0.1)
        assert len(frames) == 2
        assert frames[1][0] - started < timeout - 0.1
    else:
        await asyncio.sleep(timeout / 2)
        assert len(frames) == 1
        await asyncio.sleep(timeout / 2)
        assert len(frames) == 2
        assert frames[1][0] - started >= timeout - 0.05
    assert frames[1][1] == stop_frame
    # The stale reply did not answer the stop.
    assert not stop.done()

    os.write(drive, stop_frame)
    await asyncio.wait_for(stop, 1.0)
    asyncio.get_running_loop().remove_reader(drive)
    await controller.close()


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
