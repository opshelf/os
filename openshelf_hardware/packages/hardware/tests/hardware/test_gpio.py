from __future__ import annotations

from typing import Any

from hardware.exceptions import GpioConfigError
from hardware.gpio import DoorRelease, GpioManager
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pcf8575 import Pcf8575Backend
import hardware.gpio.rpi as rpi_module
from hardware.gpio.rpi import RpiGpioBackend
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


def test_rpi_gpio_read_recovers_when_input_channel_setup_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_gpio = FakeRpiGpioModule()
    monkeypatch.setattr(rpi_module, "GPIO", fake_gpio)
    backend = RpiGpioBackend()
    spec = GpioPinSpec(
        name="cabinet_top_closed",
        board="rpi",
        pin=17,
        direction="input",
        pull="up",
    )
    backend.setup_input(spec)
    fake_gpio.states[17] = fake_gpio.HIGH
    fake_gpio.cleanup(17)

    assert backend.read(spec) is True
    assert fake_gpio.configured[17] == fake_gpio.IN
    assert [call[0] for call in fake_gpio.setup_calls].count(17) == 2


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
