from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from hardware.gpio import GpioManager, GpioPinSpec

from openshelf_os.exceptions import ConfigError
from openshelf_os.iobox import IOBox


def make_iobox(*, gpio: GpioManager | None) -> IOBox:
    iobox = IOBox.__new__(IOBox)
    iobox._params = SimpleNamespace(
        curtain=SimpleNamespace(enabled=True, poll_interval_s=0.01),
    )
    iobox._gpio = gpio
    iobox._md = None
    iobox._logger = logging.getLogger("test_iobox_curtain_gpio")
    iobox._curtain_sensors = []
    iobox._curtain_monitors = []
    iobox._curtain_state = None
    iobox._curtain_listener = None
    iobox._door_force_refresh_on_block = False
    iobox._safety_guard_bypassed = False
    iobox._light_curtain_bypassed = False
    iobox._door_motion_lock = asyncio.Lock()
    return iobox


def make_gpio() -> GpioManager:
    return GpioManager(
        {
            "light_curtain": GpioPinSpec(
                name="light_curtain",
                board="pca9535",
                expander="pca9535_1",
                pin=1,
                direction="input",
                inverted=True,
            )
        },
        fake=True,
    )


@pytest.mark.asyncio
async def test_curtain_requires_shared_gpio_pin() -> None:
    iobox = make_iobox(gpio=None)

    with pytest.raises(ConfigError, match="shared GPIO pin 'light_curtain'"):
        await IOBox._configure_curtain(iobox)


@pytest.mark.asyncio
async def test_curtain_uses_shared_gpio_light_curtain_input() -> None:
    gpio = make_gpio()
    iobox = make_iobox(gpio=gpio)

    await IOBox._configure_curtain(iobox)
    assert iobox.get_curtain_state() is False

    gpio.set_fake_input("light_curtain", True)
    await asyncio.sleep(0)

    assert iobox.get_curtain_state() is True
    await iobox.stop()


