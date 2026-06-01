from __future__ import annotations

import asyncio

import pytest

from hardware.curtain import CurtainMonitor, CurtainSensor
from hardware.gpio import GpioManager
from hardware.gpio.types import GpioPinSpec


@pytest.mark.asyncio
async def test_curtain_monitor_uses_gpio_subscription() -> None:
    manager = GpioManager(
        {
            "light_curtain": GpioPinSpec(
                name="light_curtain",
                board="pcf8575",
                expander="main",
                pin=15,
                direction="input",
                inverted=True,
            )
        },
        fake=True,
    )
    sensor = CurtainSensor(input_device=manager.input("light_curtain"))
    changes: list[bool] = []
    monitor = CurtainMonitor(sensor, on_change=changes.append)

    await monitor.start()
    manager.set_fake_input("light_curtain", True)
    await asyncio.sleep(0)
    await monitor.stop()

    assert changes == [False, True]
