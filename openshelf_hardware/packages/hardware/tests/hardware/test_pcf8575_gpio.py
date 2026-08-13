from __future__ import annotations

from typing import Any

import pytest

from hardware.exceptions import GpioConfigError, GpioConnectionError
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.manager import GpioManager
from hardware.gpio.pcf8575 import Pcf8575Backend
import hardware.gpio.pcf8575 as pcf8575_module
from hardware.gpio.types import (
    DigitalInput,
    GpioDirection,
    GpioExpanderSpec,
    GpioInterruptSpec,
    GpioPinSpec,
    GpioSubscription,
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


def test_gpio_manager_parses_and_routes_legacy_pcf8575_config() -> None:
    config: dict[str, Any] = {
        "expanders": [
            {
                "name": "main",
                "type": "pcf8575",
                "bus": 1,
                "address": 0x20,
                "interrupt": {"board": "rpi", "pin": 4},
            }
        ],
        "pins": {
            "light_curtain": {
                "board": "pcf8575",
                "expander": "main",
                "pin": 8,
                "direction": "input",
                "inverted": True,
            }
        },
    }
    parsed = GpioManager.from_config(config)

    assert parsed._expanders["main"].kind == "pcf8575"
    assert parsed._pins["light_curtain"].board == "pcf8575"

    device = FakePcf8575()
    expander = parsed._expanders["main"]
    backend = Pcf8575Backend(
        expander,
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    manager = GpioManager(
        parsed._pins,
        parsed._expanders,
        backends={"pcf8575:main": backend},
    )

    light_curtain = manager.input("light_curtain")
    device.write_pin(8, False)

    assert light_curtain.read() is True


def test_gpio_manager_rejects_pin_and_expander_type_mismatch() -> None:
    manager = GpioManager(
        {"limit": pcf_pin("limit", 1, "input")},
        {
            "main": GpioExpanderSpec(
                name="main",
                kind="pca9535",
                bus=1,
                address=0x20,
            )
        },
    )

    with pytest.raises(
        GpioConfigError,
        match="uses board 'pcf8575'.*expander 'main' has type 'pca9535'",
    ):
        manager.input("limit")


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


def test_pcf8575_interrupt_diffs_changed_pin() -> None:
    device = FakePcf8575()
    rpi = FakeGpioBackend()
    backend = Pcf8575Backend(
        pcf_expander(interrupt=True),
        rpi_backend=rpi,
        device_factory=lambda _spec: device,
    )
    light_curtain = pcf_pin("light_curtain", 8, "input")
    interrupt = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []

    backend.setup_input(light_curtain)
    backend.subscribe(light_curtain, events.append)
    device.write_pin(8, False)
    rpi.set_raw(interrupt, False)

    assert events == [False]


@pytest.mark.parametrize(
    ("emit_initial", "expected"),
    [
        (True, [(True, True), (False, False)]),
        (False, [(False, False)]),
    ],
)
def test_pcf8575_later_subscription_reconciles_transition_consumed_by_existing_interrupt(
    emit_initial: bool,
    expected: list[tuple[bool, bool]],
) -> None:
    device = FakePcf8575()
    rpi = FakeGpioBackend()

    class TransitioningPcf8575Backend(Pcf8575Backend):
        def __init__(self) -> None:
            super().__init__(
                pcf_expander(interrupt=True),
                rpi_backend=rpi,
                device_factory=lambda _spec: device,
            )
            self.subscribe_calls = 0

        def subscribe(self, spec: GpioPinSpec, callback, **kwargs: Any) -> GpioSubscription:
            self.subscribe_calls += 1
            if self.subscribe_calls == 2:
                device.write_pin(8, False)
                rpi.set_raw(interrupt, False)
            return super().subscribe(spec, callback, **kwargs)

    backend = TransitioningPcf8575Backend()
    spec = pcf_pin("light_curtain", 8, "input")
    interrupt = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    first_events: list[bool] = []
    later_events = []

    backend.setup_input(spec)
    DigitalInput(spec, backend).subscribe(lambda event: first_events.append(event.raw_value))
    DigitalInput(spec, backend).subscribe(later_events.append, emit_initial=emit_initial)

    assert first_events == [False]
    assert [(event.value, event.raw_value) for event in later_events] == expected


@pytest.mark.parametrize("transition_timing", ["before_handler", "after_handler"])
def test_pcf8575_first_subscription_observes_transition_during_interrupt_setup(
    transition_timing: str,
) -> None:
    device = FakePcf8575()

    class TransitioningRpiBackend(FakeGpioBackend):
        def subscribe(self, spec: GpioPinSpec, callback, **kwargs: Any) -> GpioSubscription:
            if transition_timing == "before_handler":
                device.write_pin(8, False)

            subscription = super().subscribe(spec, callback, **kwargs)

            if transition_timing == "after_handler":
                device.write_pin(8, False)

            return subscription

    rpi = TransitioningRpiBackend()
    backend = Pcf8575Backend(
        pcf_expander(interrupt=True),
        rpi_backend=rpi,
        device_factory=lambda _spec: device,
    )
    spec = pcf_pin("light_curtain", 8, "input")
    interrupt = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []

    backend.setup_input(spec)
    DigitalInput(spec, backend).subscribe(lambda event: events.append(event.raw_value))

    assert events == [False]

    device.write_pin(8, True)
    rpi.set_raw(interrupt, False)

    assert events == [False, True]


def test_pcf8575_failed_first_subscription_rolls_back_interrupt_and_callback() -> None:
    class FailingReadPcf8575(FakePcf8575):
        def __init__(self) -> None:
            super().__init__()
            self.fail_read = False

        def read_gpio(self) -> int:
            if self.fail_read:
                raise OSError("read failed")
            return super().read_gpio()

    class CountingRpiBackend(FakeGpioBackend):
        def __init__(self) -> None:
            super().__init__()
            self.subscribe_calls = 0

        def subscribe(self, spec: GpioPinSpec, callback, **kwargs: Any) -> GpioSubscription:
            self.subscribe_calls += 1
            return super().subscribe(spec, callback, **kwargs)

    device = FailingReadPcf8575()
    rpi = CountingRpiBackend()
    backend = Pcf8575Backend(
        pcf_expander(interrupt=True),
        rpi_backend=rpi,
        device_factory=lambda _spec: device,
    )
    spec = pcf_pin("limit", 8, "input")
    interrupt = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")

    backend.setup_input(spec)
    device.fail_read = True

    with pytest.raises(OSError, match="read failed"):
        backend.subscribe(spec, lambda _value: None)

    assert backend._interrupt_subscription is None
    assert backend._callbacks == {}
    assert rpi._callbacks[interrupt.physical_key] == []

    device.fail_read = False
    events: list[bool] = []
    backend.subscribe(spec, events.append)
    device.write_pin(8, False)
    rpi.set_raw(interrupt, False)

    assert rpi.subscribe_calls == 2
    assert events == [False]


def test_pcf8575_poll_observation_dispatches_before_pending_interrupt() -> None:
    device = FakePcf8575()
    rpi = FakeGpioBackend()
    backend = Pcf8575Backend(
        pcf_expander(interrupt=True),
        rpi_backend=rpi,
        device_factory=lambda _spec: device,
    )
    light_curtain = pcf_pin("light_curtain", 8, "input")
    interrupt = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []

    backend.setup_input(light_curtain)
    backend.subscribe(light_curtain, events.append)
    device.write_pin(8, False)

    assert backend.read(light_curtain) is False
    rpi.set_raw(interrupt, False)

    assert events == [False]


def test_pcf8575_ignores_copied_interrupt_callback_after_close() -> None:
    class CapturingRpiBackend:
        def __init__(self) -> None:
            self.callback = None

        def setup_input(self, _spec: GpioPinSpec) -> None:
            pass

        def subscribe(self, _spec: GpioPinSpec, callback, **_kwargs: Any) -> GpioSubscription:
            self.callback = callback
            return GpioSubscription(lambda: None)

    devices: list[FakePcf8575] = []

    def create_device(_spec: GpioExpanderSpec) -> FakePcf8575:
        device = FakePcf8575()
        devices.append(device)
        return device

    rpi = CapturingRpiBackend()
    backend = Pcf8575Backend(
        pcf_expander(interrupt=True),
        rpi_backend=rpi,
        device_factory=create_device,
    )
    spec = pcf_pin("limit", 15, "input")

    backend.setup_input(spec)
    backend.subscribe(spec, lambda _value: None)
    callback = rpi.callback
    assert callback is not None

    backend.close()
    backend.close()
    callback(False)

    with pytest.raises(GpioConfigError, match="PCF8575 backend 'main' is closed"):
        backend.read(spec)

    assert len(devices) == 1
    assert backend._pcf is None


@pytest.mark.parametrize("failure_stage", ["construction", "initialization"])
def test_pcf8575_connection_failure_deinitializes_owned_i2c_bus(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    class FakeI2c:
        def __init__(self) -> None:
            self.deinit_calls = 0

        def deinit(self) -> None:
            self.deinit_calls += 1

    class FakeBusio:
        @staticmethod
        def I2C(_scl: object, _sda: object) -> FakeI2c:
            return i2c

    failure: Exception
    if failure_stage == "construction":
        failure = OSError("PCF8575 construction failed")

        class FailingPcf8575:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise failure

    else:
        failure = GpioConnectionError("PCF8575 initialization failed")

        class FailingPcf8575:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def read_gpio(self) -> int:
                raise failure

    class FakePcfModule:
        PCF8575 = FailingPcf8575

    i2c = FakeI2c()
    monkeypatch.setattr(pcf8575_module, "adafruit_pcf8575", FakePcfModule)
    monkeypatch.setattr(pcf8575_module, "board", type("FakeBoard", (), {"SCL": object(), "SDA": object()}))
    monkeypatch.setattr(pcf8575_module, "busio", FakeBusio)

    backend = Pcf8575Backend(pcf_expander(), rpi_backend=FakeGpioBackend())

    with pytest.raises(GpioConnectionError) as exc_info:
        backend.setup_output(pcf_pin("tool", 3, "output"))

    assert i2c.deinit_calls == 1
    assert backend._i2c is None
    assert backend._pcf is None
    assert backend._port_state is None
    if failure_stage == "construction":
        assert exc_info.value.__cause__ is failure
    else:
        assert exc_info.value is failure
