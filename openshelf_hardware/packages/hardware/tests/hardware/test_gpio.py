from __future__ import annotations

from typing import Any

from hardware.exceptions import GpioConfigError
from hardware.gpio import DoorRelease, GpioManager
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pca9535 import Pca9535Backend
import hardware.gpio.rpi as rpi_module
from hardware.gpio.rpi import RpiGpioBackend
from hardware.gpio.types import (
    GpioBoard,
    GpioExpanderSpec,
    GpioInterruptSpec,
    GpioPinSpec,
)

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


@pytest.mark.parametrize(
    ("board", "expander", "expanders", "error"),
    [
        (
            "pca9535",
            "main",
            {
                "main": GpioExpanderSpec(
                    name="main", kind="pcf8575", bus=1, address=0x20
                )
            },
            "uses board 'pca9535'.*expander 'main' has type 'pcf8575'",
        ),
        (
            "pcf8575",
            "main",
            {
                "main": GpioExpanderSpec(
                    name="main", kind="pca9535", bus=1, address=0x20
                )
            },
            "uses board 'pcf8575'.*expander 'main' has type 'pca9535'",
        ),
    ],
)
def test_fake_gpio_validates_expander_assignments(
    board: GpioBoard,
    expander: str | None,
    expanders: dict[str, GpioExpanderSpec],
    error: str,
) -> None:
    manager = GpioManager(
        {
            "sensor": GpioPinSpec(
                name="sensor",
                board=board,
                expander=expander,
                pin=1,
                direction="input",
            )
        },
        expanders,
        fake=True,
    )

    with pytest.raises(GpioConfigError, match=error):
        manager.input("sensor")


def test_gpio_manager_rejects_duplicate_physical_pins() -> None:
    with pytest.raises(GpioConfigError):
        GpioManager(
            {
                "a": GpioPinSpec(name="a", board="rpi", pin=1, direction="input"),
                "b": GpioPinSpec(name="b", board="rpi", pin=1, direction="input"),
            },
            fake=True,
        )


def test_gpio_manager_rejects_interrupt_pin_colliding_with_direct_rpi_pin() -> None:
    with pytest.raises(GpioConfigError):
        GpioManager(
            {"a": GpioPinSpec(name="a", board="rpi", pin=4, direction="input")},
            {
                "pca9535_0": GpioExpanderSpec(
                    name="pca9535_0",
                    kind="pca9535",
                    bus=1,
                    address=0x20,
                    interrupt=GpioInterruptSpec(board="rpi", pin=4),
                ),
            },
            fake=True,
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


def test_pca9535_setup_input_does_not_disturb_other_pins() -> None:
    device = FakePca9535()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(expander, rpi_backend=FakeGpioBackend(), device_factory=lambda _spec: device)
    spec = GpioPinSpec(
        name="limit",
        board="pca9535",
        expander="pca9535_0",
        pin=15,
        direction="input",
    )

    backend.setup_input(spec)

    assert device.directions[15] == 1
    assert device.state == 0xFFFF


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


def test_pca9535_interrupt_diffs_changed_pin() -> None:
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
        inverted=True,
    )
    interrupt_spec = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []

    backend.setup_input(spec)
    backend.subscribe(spec, events.append)
    device.set_raw(1, False)
    rpi.set_raw(interrupt_spec, False)

    assert events == [False]


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


class FakeRpiGpioModule:
    BCM = "BCM"
    IN = "IN"
    OUT = "OUT"
    HIGH = 1
    LOW = 0
    PUD_UP = "PUD_UP"
    PUD_DOWN = "PUD_DOWN"
    PUD_OFF = "PUD_OFF"
    RISING = "RISING"
    FALLING = "FALLING"
    BOTH = "BOTH"

    def __init__(self) -> None:
        self.configured: dict[int, str] = {}
        self.states: dict[int, int] = {}
        self.setup_calls: list[tuple[int, str, Any, int | None]] = []
        self.event_detect_calls: list[tuple[int, str, Any, int | None]] = []

    def setmode(self, mode: str) -> None:
        self.mode = mode

    def setwarnings(self, enabled: bool) -> None:
        self.warnings = enabled

    def setup(
        self,
        pin: int,
        direction: str,
        *,
        pull_up_down: Any = None,
        initial: int | None = None,
    ) -> None:
        self.configured[pin] = direction
        self.setup_calls.append((pin, direction, pull_up_down, initial))
        if initial is not None:
            self.states[pin] = initial
        else:
            self.states.setdefault(pin, self.LOW)

    def input(self, pin: int) -> int:
        if pin not in self.configured:
            raise RuntimeError("You must setup() the GPIO channel first")

        return self.states.get(pin, self.LOW)

    def output(self, pin: int, value: int) -> None:
        if pin not in self.configured:
            raise RuntimeError("You must setup() the GPIO channel first")

        self.states[pin] = value

    def add_event_detect(self, pin: int, edge: str, *, callback: Any, bouncetime: int | None) -> None:
        if pin not in self.configured:
            raise RuntimeError("You must setup() the GPIO channel first")

        self.event_detect_calls.append((pin, edge, callback, bouncetime))

    def remove_event_detect(self, pin: int) -> None:
        pass

    def cleanup(self, pin: int) -> None:
        self.configured.pop(pin, None)


def test_rpi_gpio_write_recovers_when_output_channel_setup_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gpio = FakeRpiGpioModule()
    monkeypatch.setattr(rpi_module, "GPIO", fake_gpio)
    backend = RpiGpioBackend()
    spec = GpioPinSpec(
        name="cabinet_top_unlock",
        board="rpi",
        pin=5,
        direction="output",
        initial=False,
    )
    backend.setup_output(spec)
    fake_gpio.cleanup(5)

    backend.write(spec, True)

    assert fake_gpio.configured[5] == fake_gpio.OUT
    assert fake_gpio.states[5] == fake_gpio.HIGH
    assert [call[0] for call in fake_gpio.setup_calls].count(5) == 2


def test_rpi_gpio_read_restores_event_detect_when_input_channel_setup_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gpio = FakeRpiGpioModule()
    monkeypatch.setattr(rpi_module, "GPIO", fake_gpio)
    backend = RpiGpioBackend()
    spec = GpioPinSpec(
        name="cabinet_top_closed",
        board="rpi",
        pin=17,
        direction="input",
        pull="up",
        debounce_ms=50,
    )
    backend.setup_input(spec)
    backend.subscribe(spec, lambda _raw: None, edge="both", debounce_ms=spec.debounce_ms)
    fake_gpio.cleanup(17)

    backend.read(spec)

    assert len(fake_gpio.event_detect_calls) == 2
    assert fake_gpio.event_detect_calls[-1][0] == 17
    assert fake_gpio.event_detect_calls[-1][1] == fake_gpio.BOTH
    assert fake_gpio.event_detect_calls[-1][3] == 50
