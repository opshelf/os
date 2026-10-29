from __future__ import annotations

import logging
import time

from hardware.gpio import manager as gpio_manager
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pca9535 import Pca9535Backend
from hardware.gpio.types import (
    GpioEvent,
    GpioExpanderSpec,
    GpioInterruptSpec,
    GpioPinSpec,
)



class FakePca9535:
    def __init__(self) -> None:
        self.state = 0xFFFF
        self.directions = [1] * 16

    def pin_config_write(self, pin: int, direction: int) -> None:
        self.directions[pin] = direction

    def pin_output_write(self, pin: int, level: int) -> None:
        self.set_raw(pin, bool(level))

    def pin_input_read(self, pin: int) -> int:
        return (self.state >> pin) & 1

    def port_input_read(self) -> tuple[int, int]:
        return self.state & 0xFF, (self.state >> 8) & 0xFF

    def set_raw(self, pin: int, value: bool) -> None:
        if value is True:
            self.state |= 1 << pin
        else:
            self.state &= ~(1 << pin)


def test_pca9535_output_writes_preserve_other_pins() -> None:
    device = FakePca9535()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(expander, rpi_backend=FakeGpioBackend(), device_factory=lambda _spec: device)
    output = GpioPinSpec(
        name="tool",
        board="pca9535",
        expander="pca9535_0",
        pin=3,
        direction="output",
        initial=False,
    )
    limit = GpioPinSpec(
        name="limit",
        board="pca9535",
        expander="pca9535_0",
        pin=15,
        direction="input",
    )

    backend.setup_output(output)
    backend.setup_input(limit)
    backend.write(output, True)

    assert device.directions[3] == 0
    assert device.directions[15] == 1
    assert device.pin_input_read(15) == 1
    assert device.pin_input_read(3) == 1


def test_pca9535_poll_read_updates_interrupt_baseline() -> None:
    device = FakePca9535()
    rpi = FakeGpioBackend()
    expander = GpioExpanderSpec(
        name="pca9535_1",
        kind="pca9535",
        bus=1,
        address=0x21,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    backend = Pca9535Backend(expander, rpi_backend=rpi, device_factory=lambda _spec: device)
    spec = GpioPinSpec(
        name="light_curtain",
        board="pca9535",
        expander="pca9535_1",
        pin=1,
        direction="input",
    )
    interrupt_spec = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []

    backend.setup_input(spec)
    backend.subscribe(spec, events.append)
    device.set_raw(1, False)

    assert backend.read(spec) is False

    device.set_raw(1, True)
    rpi.set_raw(interrupt_spec, False)

    assert events == [False, True]


def test_pca9535_shared_interrupt_line_dispatches_both_expanders() -> None:
    device0 = FakePca9535()
    device1 = FakePca9535()
    rpi = FakeGpioBackend()
    expander0 = GpioExpanderSpec(
        name="pca9535_0",
        kind="pca9535",
        bus=1,
        address=0x20,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    expander1 = GpioExpanderSpec(
        name="pca9535_1",
        kind="pca9535",
        bus=1,
        address=0x21,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    backend0 = Pca9535Backend(expander0, rpi_backend=rpi, device_factory=lambda _spec: device0)
    backend1 = Pca9535Backend(expander1, rpi_backend=rpi, device_factory=lambda _spec: device1)
    spec0 = GpioPinSpec(name="a", board="pca9535", expander="pca9535_0", pin=1, direction="input", inverted=True)
    spec1 = GpioPinSpec(name="b", board="pca9535", expander="pca9535_1", pin=2, direction="input", inverted=True)
    interrupt_spec = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events0: list[bool] = []
    events1: list[bool] = []

    backend0.setup_input(spec0)
    backend0.subscribe(spec0, events0.append)
    backend1.setup_input(spec1)
    backend1.subscribe(spec1, events1.append)

    device0.set_raw(1, False)
    device1.set_raw(2, False)
    rpi.set_raw(interrupt_spec, False)

    assert events0 == [False]
    assert events1 == [False]


def test_input_change_log_ends_a_clean_edge_pair_with_the_final_state(monkeypatch, caplog) -> None:
    # Only the first edge used to be logged, so the log showed the pin stuck in the wrong state.
    monkeypatch.setattr(gpio_manager, "INPUT_LOG_WINDOW_S", 0.05)
    log = gpio_manager._InputChangeLog()
    caplog.set_level(logging.INFO, logger=gpio_manager.logger.name)

    for value in (False, True):
        log(GpioEvent(name="estop", value=value, raw_value=value, timestamp=0.0))

    deadline = time.monotonic() + 2.0
    while len(caplog.records) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    log.close()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["estop: LOW", "estop: HIGH (1 more change since the last line)"]
