from __future__ import annotations

from hardware.exceptions import GpioConfigError
from hardware.gpio import DoorRelease, GpioManager
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pcf8575 import Pcf8575Backend
from hardware.gpio.types import GpioExpanderSpec, GpioInterruptSpec, GpioPinSpec

import pytest


def test_fake_gpio_input_inversion_and_subscription() -> None:
    manager = GpioManager(
        {
            "sensor": GpioPinSpec(
                name="sensor",
                board="rpi",
                pin=1,
                direction="input",
                inverted=True,
            )
        },
        fake=True,
    )
    sensor = manager.input("sensor")
    events = []

    subscription = sensor.subscribe(events.append, emit_initial=True)

    assert events[-1].value is False

    manager.set_fake_input("sensor", True)

    assert events[-1].value is True
    assert events[-1].raw_value is False

    subscription.close()
    manager.set_fake_input("sensor", False)

    assert events[-1].value is True


def test_fake_gpio_output_initial_write_and_shutdown() -> None:
    manager = GpioManager(
        {
            "unlock": GpioPinSpec(
                name="unlock",
                board="rpi",
                pin=5,
                direction="output",
                initial=False,
                shutdown=False,
            )
        },
        fake=True,
    )
    unlock = manager.output("unlock")

    assert unlock.read() is False

    unlock.write(True)

    assert unlock.read() is True

    manager.close()

    assert unlock.read() is False


def test_gpio_manager_applies_only_declared_initial_outputs() -> None:
    manager = GpioManager(
        {
            "unlock": GpioPinSpec(
                name="unlock",
                board="rpi",
                pin=5,
                direction="output",
                initial=False,
                shutdown=False,
            ),
            "standby": GpioPinSpec(name="standby", board="rpi", pin=23, direction="output"),
        },
        fake=True,
    )

    manager.apply_initial_outputs()

    assert manager.output("unlock").read() is False
    assert "standby" not in manager._outputs

def test_gpio_manager_rejects_duplicate_physical_pins() -> None:
    with pytest.raises(GpioConfigError):
        GpioManager(
            {
                "a": GpioPinSpec(name="a", board="rpi", pin=1, direction="input"),
                "b": GpioPinSpec(name="b", board="rpi", pin=1, direction="input"),
            },
            fake=True,
        )


class FakePcf8575:
    def __init__(self) -> None:
        self.state = 0xFFFF
        self._writebuf = bytearray([0, 0])

    def read_gpio(self) -> int:
        return self.state

    def write_gpio(self, value: int) -> None:
        self._writebuf[0] = value & 0xFF
        self._writebuf[1] = (value >> 8) & 0xFF
        self.state = value & 0xFFFF

    def write_pin(self, pin: int, value: bool) -> None:
        latch = self._writebuf[0] | (self._writebuf[1] << 8)
        if value is True:
            latch |= 1 << pin
        else:
            latch &= ~(1 << pin)
        self.write_gpio(latch)


def test_pcf8575_setup_input_releases_pin_without_clearing_other_pins() -> None:
    pcf = FakePcf8575()
    expander = GpioExpanderSpec(name="main", kind="pcf8575", bus=1, address=0x20)
    backend = Pcf8575Backend(expander, rpi_backend=FakeGpioBackend(), device_factory=lambda _spec: pcf)
    spec = GpioPinSpec(
        name="limit",
        board="pcf8575",
        expander="main",
        pin=15,
        direction="input",
    )

    backend.setup_input(spec)

    assert pcf.state == 0xFFFF


def test_pcf8575_output_writes_preserve_other_released_pins() -> None:
    pcf = FakePcf8575()
    expander = GpioExpanderSpec(name="main", kind="pcf8575", bus=1, address=0x20)
    backend = Pcf8575Backend(expander, rpi_backend=FakeGpioBackend(), device_factory=lambda _spec: pcf)
    output = GpioPinSpec(
        name="tool",
        board="pcf8575",
        expander="main",
        pin=3,
        direction="output",
        initial=False,
    )
    limit = GpioPinSpec(
        name="limit",
        board="pcf8575",
        expander="main",
        pin=15,
        direction="input",
    )

    backend.setup_output(output)
    backend.setup_input(limit)
    backend.write(output, True)

    assert pcf.state == 0xFFFF


def test_pcf8575_interrupt_diffs_changed_pin() -> None:
    pcf = FakePcf8575()
    rpi = FakeGpioBackend()
    expander = GpioExpanderSpec(
        name="main",
        kind="pcf8575",
        bus=1,
        address=0x20,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    backend = Pcf8575Backend(expander, rpi_backend=rpi, device_factory=lambda _spec: pcf)
    spec = GpioPinSpec(
        name="light_curtain",
        board="pcf8575",
        expander="main",
        pin=15,
        direction="input",
        inverted=True,
    )
    interrupt_spec = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []

    backend.setup_input(spec)
    backend.subscribe(spec, events.append)
    pcf.write_pin(15, False)
    rpi.set_raw(interrupt_spec, False)

    assert events == [False]


def test_door_release_uses_unlock_output_and_engaged_sensor() -> None:
    manager = GpioManager(
        {
            "cabinet_unlock": GpioPinSpec(
                name="cabinet_unlock",
                board="rpi",
                pin=5,
                direction="output",
                initial=False,
                shutdown=False,
            ),
            "cabinet_engaged": GpioPinSpec(
                name="cabinet_engaged",
                board="rpi",
                pin=17,
                direction="input",
                inverted=True,
            ),
        },
        fake=True,
    )
    unlock = manager.output("cabinet_unlock")
    engaged = manager.input("cabinet_engaged")
    release = DoorRelease(unlock_output=unlock, engaged_input=engaged)

    manager.set_fake_input("cabinet_engaged", True)

    assert release.is_engaged() is True
    assert unlock.read() is False

    release.release()

    assert unlock.read() is True

    release.set_passive()

    assert unlock.read() is False
