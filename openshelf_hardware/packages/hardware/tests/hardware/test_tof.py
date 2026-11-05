from __future__ import annotations

from typing import Any

import adafruit_tca9548a
from adafruit_bus_device.i2c_device import I2CDevice

from hardware.tof import _FtdiI2CBus


class FakeFtdiI2C:
    def __init__(self) -> None:
        self.writes: list[tuple[int, bytes]] = []

    def writeto(
        self,
        address: int,
        buffer: Any,
        *,
        start: int = 0,
        end: int | None = None,
        stop: bool = True,
    ) -> None:
        self.writes.append((address, bytes(buffer[start:end])))


def test_adafruit_drivers_can_lock_the_ftdi_bus_through_the_mux() -> None:
    # The adafruit mux and sensor drivers lock the bus by duck typing, so a missing
    # try_lock only fails on hardware, where the sizer reports the ToF as unavailable.
    raw = FakeFtdiI2C()
    bus = _FtdiI2CBus(raw)
    mux = adafruit_tca9548a.TCA9548A(bus, address=0x70)

    I2CDevice(mux[2], 0x29)

    assert raw.writes[0] == (0x70, bytes([1 << 2]))
    assert (0x29, b"") in raw.writes
    assert bus._lock.locked() is False
