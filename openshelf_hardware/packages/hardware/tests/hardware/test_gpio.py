from __future__ import annotations

import threading
from typing import Any

from hardware.exceptions import GpioConfigError, GpioConnectionError
from hardware.gpio import DoorRelease, GpioManager
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pca9535 import Pca9535Backend
import hardware.gpio.pca9535 as pca9535_module
import hardware.gpio.pca9535_mod as pca9535_mod
import hardware.gpio.rpi as rpi_module
from hardware.gpio.rpi import RpiGpioBackend
from hardware.gpio.types import (
    DigitalInput,
    GpioBoard,
    GpioExpanderSpec,
    GpioInterruptSpec,
    GpioPinSpec,
    GpioSubscription,
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


@pytest.mark.parametrize(
    ("emit_initial", "debounce_ms", "expected"),
    [
        (True, 50, [(False, True), (True, False)]),
        (False, 50, [(True, False)]),
    ],
)
def test_digital_input_buffers_synchronous_subscribe_changes_until_after_initial(
    emit_initial: bool,
    debounce_ms: int,
    expected: list[tuple[bool, bool]],
) -> None:
    class SynchronouslyChangingBackend(FakeGpioBackend):
        def subscribe(self, spec: GpioPinSpec, callback, **kwargs: Any) -> GpioSubscription:
            subscription = super().subscribe(spec, callback, **kwargs)
            self.set_raw(spec, False)
            return subscription

    spec = GpioPinSpec(
        name="sensor",
        board="rpi",
        pin=1,
        direction="input",
        inverted=True,
        debounce_ms=debounce_ms,
    )
    backend = SynchronouslyChangingBackend()
    backend.setup_input(spec)
    events = []

    DigitalInput(spec, backend).subscribe(events.append, emit_initial=emit_initial)

    assert [(event.value, event.raw_value) for event in events] == expected


def test_digital_input_closes_backend_subscription_if_post_subscription_read_fails() -> None:
    class FailingPostSubscriptionReadBackend(FakeGpioBackend):
        def __init__(self) -> None:
            super().__init__()
            self.read_calls = 0

        def read(self, spec: GpioPinSpec) -> bool:
            self.read_calls += 1
            if self.read_calls == 2:
                raise RuntimeError("post-subscription read failed")
            return super().read(spec)

    spec = GpioPinSpec(name="sensor", board="rpi", pin=1, direction="input")
    backend = FailingPostSubscriptionReadBackend()
    backend.setup_input(spec)

    with pytest.raises(RuntimeError, match="post-subscription read failed"):
        DigitalInput(spec, backend).subscribe(lambda _event: None)

    assert backend._callbacks[spec.physical_key] == []


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


def test_fake_gpio_allows_omitted_expander_topology_on_one_shared_backend() -> None:
    manager = GpioManager(
        {
            "pca_sensor": GpioPinSpec(
                name="pca_sensor",
                board="pca9535",
                expander="pca",
                pin=1,
                direction="input",
            ),
            "pcf_sensor": GpioPinSpec(
                name="pcf_sensor",
                board="pcf8575",
                expander="pcf",
                pin=2,
                direction="input",
            ),
        },
        fake=True,
    )

    pca_sensor = manager.input("pca_sensor")
    pcf_sensor = manager.input("pcf_sensor")

    assert isinstance(pca_sensor._backend, FakeGpioBackend)
    assert pca_sensor._backend is pcf_sensor._backend


def test_gpio_manager_logs_input_changes_after_initialization(caplog: pytest.LogCaptureFixture) -> None:
    manager = GpioManager(
        {
            "sensor": GpioPinSpec(
                name="sensor",
                board="rpi",
                pin=1,
                direction="input",
            )
        },
        fake=True,
    )

    with caplog.at_level("INFO", logger="hardware.gpio.manager"):
        manager.input("sensor")
        manager.set_fake_input("sensor", False)

    assert "sensor: LOW" in caplog.text
    assert "sensor: HIGH" not in caplog.text


def test_gpio_manager_observe_all_inputs_opens_every_input_pin() -> None:
    manager = GpioManager(
        {
            "sensor_a": GpioPinSpec(name="sensor_a", board="rpi", pin=1, direction="input"),
            "sensor_b": GpioPinSpec(name="sensor_b", board="rpi", pin=2, direction="input"),
            "actuator": GpioPinSpec(name="actuator", board="rpi", pin=3, direction="output"),
        },
        fake=True,
    )

    manager.observe_all_inputs()

    assert "sensor_a" in manager._inputs
    assert "sensor_b" in manager._inputs
    assert "actuator" not in manager._outputs


def test_gpio_manager_observe_all_inputs_logs_changes_without_manual_input_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = GpioManager(
        {
            "sensor": GpioPinSpec(name="sensor", board="rpi", pin=1, direction="input"),
        },
        fake=True,
    )

    manager.observe_all_inputs()

    with caplog.at_level("INFO", logger="hardware.gpio.manager"):
        manager.set_fake_input("sensor", False)

    assert "sensor: LOW" in caplog.text


def test_gpio_manager_observe_all_inputs_allows_polling_only_pca9535(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = FakePca9535()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(
        expander,
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    manager = GpioManager(
        {
            "limit": GpioPinSpec(
                name="limit",
                board="pca9535",
                expander="pca9535_0",
                pin=15,
                direction="input",
            )
        },
        {"pca9535_0": expander},
        backends={"pca9535:pca9535_0": backend},
    )

    with caplog.at_level("WARNING", logger="hardware.gpio.manager"):
        manager.observe_all_inputs()

    assert manager.input("limit").read() is True
    assert "Failed to subscribe to GPIO input 'limit' for diagnostic logging" in caplog.text


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


def test_gpio_manager_allows_expanders_to_share_one_interrupt_pin() -> None:
    GpioManager(
        {},
        {
            "pca9535_0": GpioExpanderSpec(
                name="pca9535_0",
                kind="pca9535",
                bus=1,
                address=0x20,
                interrupt=GpioInterruptSpec(board="rpi", pin=4),
            ),
            "pca9535_1": GpioExpanderSpec(
                name="pca9535_1",
                kind="pca9535",
                bus=1,
                address=0x21,
                interrupt=GpioInterruptSpec(board="rpi", pin=4),
            ),
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


class ClosableFakePca9535(FakePca9535):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FailingSetupFakePca9535(ClosableFakePca9535):
    def __init__(self) -> None:
        super().__init__()
        self.config_error: Exception | None = None
        self.close_error: Exception | None = None
        self.output_write_calls = 0

    def pin_config_write(self, pin: int, direction: int) -> None:
        if self.config_error is not None:
            raise self.config_error
        super().pin_config_write(pin, direction)

    def pin_output_write(self, pin: int, level: int) -> None:
        self.output_write_calls += 1
        super().pin_output_write(pin, level)

    def close(self) -> None:
        super().close()
        if self.close_error is not None:
            raise self.close_error


class FakePca9535Bus:
    def __init__(self, *, write_error: Exception | None = None, close_error: Exception | None = None) -> None:
        self.write_error = write_error
        self.close_error = close_error
        self.close_calls = 0

    def write_i2c_block_data(self, *_args: Any) -> None:
        if self.write_error is not None:
            raise self.write_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class FakeSmbusModule:
    def __init__(self, bus: FakePca9535Bus) -> None:
        self.bus = bus

    def SMBus(self, _bus_num: int) -> FakePca9535Bus:
        return self.bus


def test_pca9535_closes_owned_bus_idempotently(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakePca9535Bus()
    monkeypatch.setattr(pca9535_mod, "smbus", FakeSmbusModule(bus))

    device = pca9535_mod.PCA9535()

    device.close()
    device.close()

    assert bus.close_calls == 1


def test_pca9535_close_still_closes_bus_after_interrupt_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingInterruptGpio:
        def remove_event_detect(self, _pin: int) -> None:
            raise RuntimeError("interrupt cleanup failed")

    bus = FakePca9535Bus()
    monkeypatch.setattr(pca9535_mod, "smbus", FakeSmbusModule(bus))
    monkeypatch.setattr(pca9535_mod, "GPIO", FailingInterruptGpio())
    device = pca9535_mod.PCA9535()
    device.interrupt_pin = 4
    device._interrupt_registered = True

    with pytest.raises(RuntimeError, match="interrupt cleanup failed"):
        device.close()

    assert bus.close_calls == 1


def test_pca9535_constructor_closes_bus_when_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_error = OSError("initialization write failed")
    bus = FakePca9535Bus(write_error=write_error, close_error=RuntimeError("close failed"))
    monkeypatch.setattr(pca9535_mod, "smbus", FakeSmbusModule(bus))

    with pytest.raises(OSError, match="initialization write failed"):
        pca9535_mod.PCA9535(port_output_value=[0x00, 0x00])

    assert bus.close_calls == 1


def test_pca9535_backend_close_closes_device_idempotently() -> None:
    device = ClosableFakePca9535()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(
        expander,
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    spec = GpioPinSpec(name="limit", board="pca9535", expander="pca9535_0", pin=15, direction="input")

    backend.setup_input(spec)
    backend.close()
    backend.close()

    assert device.close_calls == 1


@pytest.mark.parametrize("direction", ["input", "output"])
def test_pca9535_setup_write_failure_closes_terminal_backend(direction: str) -> None:
    device = FailingSetupFakePca9535()
    rpi = FakeGpioBackend()
    expander = GpioExpanderSpec(
        name="pca9535_0",
        kind="pca9535",
        bus=1,
        address=0x20,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    backend = Pca9535Backend(
        expander,
        rpi_backend=rpi,
        device_factory=lambda _spec: device,
    )
    input_spec = GpioPinSpec(
        name="limit",
        board="pca9535",
        expander="pca9535_0",
        pin=15,
        direction="input",
    )
    output_spec = GpioPinSpec(
        name="tool",
        board="pca9535",
        expander="pca9535_0",
        pin=3,
        direction="output",
        initial=True,
    )

    backend.setup_input(input_spec)
    backend.subscribe(input_spec, lambda _raw: None)
    setup_spec = input_spec if direction == "input" else output_spec
    write_error = OSError(f"{direction} setup write failed")
    device.config_error = write_error

    with pytest.raises(GpioConnectionError) as exc_info:
        if direction == "input":
            backend.setup_input(setup_spec)
        else:
            backend.setup_output(setup_spec)

    assert exc_info.value.__cause__ is write_error
    assert device.close_calls == 1
    assert backend._closed is True
    assert backend._device is None
    assert backend._port_state is None
    assert backend._interrupt_subscription is None
    assert backend._callbacks == {}
    assert rpi._callbacks[("rpi", None, 4)] == []
    if direction == "output":
        assert device.output_write_calls == 1


def test_pca9535_setup_failure_preserves_write_error_when_cleanup_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = FailingSetupFakePca9535()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(
        expander,
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    spec = GpioPinSpec(name="limit", board="pca9535", expander="pca9535_0", pin=15, direction="input")
    write_error = OSError("input setup write failed")
    device.config_error = write_error
    device.close_error = RuntimeError("device cleanup failed")

    with caplog.at_level("WARNING", logger="hardware.gpio.pca9535.pca9535"):
        with pytest.raises(GpioConnectionError) as exc_info:
            backend.setup_input(spec)

    assert exc_info.value.__cause__ is write_error
    assert device.close_calls == 1
    cleanup_record = next(
        record for record in caplog.records if record.message == "Failed to clean up PCA9535 after pin setup failure"
    )
    assert cleanup_record.expander == "pca9535_0"
    assert cleanup_record.pin == "limit"
    assert cleanup_record.pin_number == 15


def test_pca9535_backend_ignores_copied_interrupt_callback_after_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class CapturingRpiBackend:
        def __init__(self) -> None:
            self.callback = None

        def setup_input(self, _spec: GpioPinSpec) -> None:
            pass

        def subscribe(self, _spec: GpioPinSpec, callback, **_kwargs: Any) -> GpioSubscription:
            self.callback = callback
            return GpioSubscription(lambda: None)

    devices: list[ClosableFakePca9535] = []

    def create_device(_spec: GpioExpanderSpec) -> ClosableFakePca9535:
        device = ClosableFakePca9535()
        devices.append(device)
        return device

    rpi = CapturingRpiBackend()
    expander = GpioExpanderSpec(
        name="pca9535_0",
        kind="pca9535",
        bus=1,
        address=0x20,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    backend = Pca9535Backend(expander, rpi_backend=rpi, device_factory=create_device)
    spec = GpioPinSpec(name="limit", board="pca9535", expander="pca9535_0", pin=15, direction="input")

    backend.setup_input(spec)
    backend.subscribe(spec, lambda _value: None)
    callback = rpi.callback
    assert callback is not None

    backend.close()

    with caplog.at_level("WARNING"):
        callback(False)

    with pytest.raises(GpioConfigError, match="PCA9535 backend 'pca9535_0' is closed"):
        backend.read(spec)

    assert len(devices) == 1
    assert devices[0].close_calls == 1
    assert backend._device is None
    assert caplog.records == []


def test_pca9535_backend_close_attempts_device_cleanup_after_interrupt_failure() -> None:
    device = ClosableFakePca9535()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(
        expander,
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    spec = GpioPinSpec(name="limit", board="pca9535", expander="pca9535_0", pin=15, direction="input")

    backend.setup_input(spec)

    def fail_to_unsubscribe() -> None:
        raise RuntimeError("interrupt cleanup failed")

    backend._interrupt_subscription = GpioSubscription(fail_to_unsubscribe)

    with pytest.raises(RuntimeError, match="interrupt cleanup failed"):
        backend.close()

    assert device.close_calls == 1
    assert backend._interrupt_subscription is None
    assert backend._device is None
    assert backend._port_state is None


def test_gpio_manager_propagates_pca9535_close_failure() -> None:
    class FailingCloseDevice(FakePca9535):
        def close(self) -> None:
            raise RuntimeError("device cleanup failed")

    device = FailingCloseDevice()
    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(
        expander,
        rpi_backend=FakeGpioBackend(),
        device_factory=lambda _spec: device,
    )
    manager = GpioManager(
        {
            "limit": GpioPinSpec(
                name="limit",
                board="pca9535",
                expander="pca9535_0",
                pin=15,
                direction="input",
            )
        },
        {"pca9535_0": expander},
        backends={"pca9535:pca9535_0": backend},
    )

    manager.input("limit")

    with pytest.raises(GpioConfigError) as exc_info:
        manager.close()

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert manager._closed is True


def test_pca9535_backend_closes_device_when_polarity_reset_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPolarityDevice:
        def __init__(self) -> None:
            self.close_calls = 0

        def port_invert_write(self, _port0: int, _port1: int) -> None:
            raise OSError("polarity reset failed")

        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("device close failed")

    device = FailingPolarityDevice()
    monkeypatch.setattr(pca9535_module, "PCA9535", lambda **_kwargs: device)

    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(expander, rpi_backend=FakeGpioBackend())
    spec = GpioPinSpec(name="limit", board="pca9535", expander="pca9535_0", pin=15, direction="input")

    with pytest.raises(GpioConnectionError) as exc_info:
        backend.setup_input(spec)

    assert isinstance(exc_info.value.__cause__, OSError)
    assert device.close_calls == 1


def test_pca9535_wraps_device_construction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising_device(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("No such device")

    monkeypatch.setattr(pca9535_module, "PCA9535", raising_device)

    expander = GpioExpanderSpec(name="pca9535_0", kind="pca9535", bus=1, address=0x20)
    backend = Pca9535Backend(expander, rpi_backend=FakeGpioBackend())
    spec = GpioPinSpec(name="limit", board="pca9535", expander="pca9535_0", pin=15, direction="input")

    with pytest.raises(GpioConnectionError, match="Failed to connect PCA9535 expander 'pca9535_0'") as exc_info:
        backend.setup_input(spec)

    assert isinstance(exc_info.value.__cause__, OSError)


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


def test_pca9535_poll_read_dispatches_changed_subscribed_sibling() -> None:
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
    polled_spec = GpioPinSpec(
        name="polled",
        board="pca9535",
        expander="pca9535_1",
        pin=1,
        direction="input",
    )
    subscribed_spec = GpioPinSpec(
        name="subscribed",
        board="pca9535",
        expander="pca9535_1",
        pin=2,
        direction="input",
    )
    events: list[bool] = []

    backend.setup_input(polled_spec)
    backend.setup_input(subscribed_spec)
    backend.subscribe(subscribed_spec, events.append)
    device.set_raw(2, False)

    assert backend.read(polled_spec) is True
    assert events == [False]


def test_pca9535_serializes_overlapping_poll_and_interrupt_callback_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    first_dispatch_started = threading.Event()
    release_first_dispatch = threading.Event()
    original_dispatch = backend._dispatch_callbacks

    def pause_first_dispatch(pending: list[tuple[list[Any], bool]]) -> None:
        if not first_dispatch_started.is_set():
            first_dispatch_started.set()
            assert release_first_dispatch.wait(timeout=1)
        original_dispatch(pending)

    monkeypatch.setattr(backend, "_dispatch_callbacks", pause_first_dispatch)
    device.set_raw(1, False)
    backend.setup_input(spec)
    backend.subscribe(spec, events.append)

    device.set_raw(1, True)
    poll_thread = threading.Thread(target=backend.read, args=(spec,))
    poll_thread.start()
    assert first_dispatch_started.wait(timeout=1)

    device.set_raw(1, False)
    rpi.set_raw(interrupt_spec, False)
    release_first_dispatch.set()
    poll_thread.join(timeout=1)

    assert not poll_thread.is_alive()
    assert events == [True, False]


def test_pca9535_callback_failure_drains_overlapping_callback_batch() -> None:
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
    errors: list[BaseException] = []
    first_callback_started = threading.Event()
    later_observation_queued = threading.Event()
    callback_error = RuntimeError("callback failed")

    def fail_after_later_observation(raw_value: bool) -> None:
        if raw_value:
            first_callback_started.set()
            assert later_observation_queued.wait(timeout=1)
            raise callback_error

    def poll() -> None:
        try:
            backend.read(spec)
        except BaseException as exc:
            errors.append(exc)

    device.set_raw(1, False)
    backend.setup_input(spec)
    backend.subscribe(spec, events.append)
    backend.subscribe(spec, fail_after_later_observation)

    device.set_raw(1, True)
    poll_thread = threading.Thread(target=poll)
    poll_thread.start()
    assert first_callback_started.wait(timeout=1)

    device.set_raw(1, False)
    rpi.set_raw(interrupt_spec, False)
    later_observation_queued.set()
    poll_thread.join(timeout=1)

    assert not poll_thread.is_alive()
    assert errors == [callback_error]
    assert events == [True, False]
    assert not backend._pending_callback_batches
    assert backend._dispatching_callbacks is False


def test_pca9535_close_during_callback_dispatch_drains_preclose_batches_fifo() -> None:
    device = ClosableFakePca9535()
    rpi = FakeGpioBackend()
    expander = GpioExpanderSpec(
        name="pca9535_1",
        kind="pca9535",
        bus=1,
        address=0x21,
        interrupt=GpioInterruptSpec(board="rpi", pin=4),
    )
    device_factory_calls = 0

    def create_device(_spec: GpioExpanderSpec) -> ClosableFakePca9535:
        nonlocal device_factory_calls
        device_factory_calls += 1
        return device

    backend = Pca9535Backend(expander, rpi_backend=rpi, device_factory=create_device)
    spec = GpioPinSpec(
        name="light_curtain",
        board="pca9535",
        expander="pca9535_1",
        pin=1,
        direction="input",
    )
    interrupt_spec = GpioPinSpec(name="interrupt", board="rpi", pin=4, direction="input")
    events: list[bool] = []
    errors: list[BaseException] = []
    first_callback_started = threading.Event()
    later_observation_queued = threading.Event()
    callback_error = RuntimeError("callback failed")
    later_callback_error = RuntimeError("later callback failed")

    def close_and_fail_after_later_observation(raw_value: bool) -> None:
        if raw_value:
            first_callback_started.set()
            assert later_observation_queued.wait(timeout=1)
            backend.close()
            raise callback_error
        raise later_callback_error

    def poll() -> None:
        try:
            backend.read(spec)
        except BaseException as exc:
            errors.append(exc)

    device.set_raw(1, False)
    backend.setup_input(spec)
    backend.subscribe(spec, events.append)
    backend.subscribe(spec, close_and_fail_after_later_observation)

    device.set_raw(1, True)
    poll_thread = threading.Thread(target=poll)
    poll_thread.start()
    assert first_callback_started.wait(timeout=1)

    device.set_raw(1, False)
    rpi.set_raw(interrupt_spec, False)
    later_observation_queued.set()
    poll_thread.join(timeout=1)

    assert not poll_thread.is_alive()
    assert errors == [callback_error]
    assert events == [True, False]
    assert not backend._pending_callback_batches
    assert backend._dispatching_callbacks is False
    assert backend._closed is True
    assert backend._callbacks == {}
    assert backend._device is None
    assert device.close_calls == 1

    backend._did_interrupt()
    with pytest.raises(GpioConfigError, match="PCA9535 backend 'pca9535_1' is closed"):
        backend.read(spec)

    assert device_factory_calls == 1


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


def test_rpi_gpio_reuses_shared_input_without_disabling_event_detect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gpio = FakeRpiGpioModule()
    monkeypatch.setattr(rpi_module, "GPIO", fake_gpio)
    backend = RpiGpioBackend()
    first = GpioPinSpec(
        name="pca9535_0_interrupt",
        board="rpi",
        pin=4,
        direction="input",
        pull="up",
    )
    second = GpioPinSpec(
        name="pca9535_1_interrupt",
        board="rpi",
        pin=4,
        direction="input",
        pull="up",
    )

    backend.setup_input(first)
    backend.subscribe(first, lambda _raw: None, edge="falling")
    backend.setup_input(second)
    backend.subscribe(second, lambda _raw: None, edge="falling")

    assert [call[0] for call in fake_gpio.setup_calls].count(4) == 1
    assert [call[0] for call in fake_gpio.event_detect_calls].count(4) == 1


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
