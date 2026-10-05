from __future__ import annotations

from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pcf8575 import Pcf8575Backend
from hardware.gpio.types import (
    GpioDirection,
    GpioExpanderSpec,
    GpioInterruptSpec,
    GpioPinSpec,
)


class FakePcf8575:
    def __init__(self) -> None:
        self.state = 0xFFFF
        self.writes: list[int] = []

    def read_gpio(self) -> int:
        return self.state

    def write_gpio(self, value: int) -> None:
        self.state = value & 0xFFFF
        self.writes.append(self.state)

    def write_pin(self, pin: int, value: bool) -> None:
        if value is True:
            self.state |= 1 << pin
        else:
            self.state &= ~(1 << pin)


def pcf_expander(*, interrupt: bool = False) -> GpioExpanderSpec:
    return GpioExpanderSpec(
        name="main",
        kind="pcf8575",
        bus=1,
        address=0x20,
        interrupt=GpioInterruptSpec(board="rpi", pin=4) if interrupt else None,
    )


def pcf_pin(
    name: str,
    pin: int,
    direction: GpioDirection,
    *,
    initial: bool | None = None,
) -> GpioPinSpec:
    return GpioPinSpec(
        name=name,
        board="pcf8575",
        expander="main",
        pin=pin,
        direction=direction,
        initial=initial,
    )


def test_pcf8575_output_writes_preserve_released_input_pins() -> None:
    device = FakePcf8575()
    backend = Pcf8575Backend(
        pcf_expander(),
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    output = pcf_pin("tool", 3, "output", initial=False)
    limit = pcf_pin("limit", 15, "input")

    backend.setup_output(output)
    backend.setup_input(limit)
    backend.write(output, True)

    assert device.writes == [0xFFF7, 0xFFF7, 0xFFFF]


