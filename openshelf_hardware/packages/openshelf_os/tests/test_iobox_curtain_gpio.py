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
    iobox._curtain_input_states = {}
    iobox._curtain_state = None
    iobox._curtain_listener = None
    iobox._door_force_refresh_on_block = False
    return iobox


def make_gpio(*pin_names: str) -> GpioManager:
    return GpioManager(
        {
            pin_name: GpioPinSpec(
                name=pin_name,
                board="pcf8575",
                expander="main",
                pin=15 if pin_name != "light_curtain_2" else 14,
                direction="input",
                inverted=True,
            )
            for pin_name in pin_names
        },
        fake=True,
    )


@pytest.mark.asyncio
async def test_curtain_requires_shared_gpio_pin() -> None:
    iobox = make_iobox(gpio=None)

    with pytest.raises(ConfigError, match="shared GPIO pin 'light_curtain_1'"):
        await IOBox._configure_curtain(iobox)


@pytest.mark.asyncio
async def test_curtain_blocks_when_either_shared_gpio_input_is_blocked() -> None:
    gpio = make_gpio("light_curtain_1", "light_curtain_2")
    iobox = make_iobox(gpio=gpio)

    await IOBox._configure_curtain(iobox)
    assert iobox.get_curtain_state() is False

    gpio.set_fake_input("light_curtain_2", True)
    await asyncio.sleep(0)
    assert iobox.get_curtain_state() is True

    gpio.set_fake_input("light_curtain_2", False)
    await asyncio.sleep(0)
    assert iobox.get_curtain_state() is False

    gpio.set_fake_input("light_curtain_1", True)
    await asyncio.sleep(0)
    assert iobox.get_curtain_state() is True

    await iobox.stop()


