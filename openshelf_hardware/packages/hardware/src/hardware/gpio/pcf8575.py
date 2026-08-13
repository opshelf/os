from __future__ import annotations

import importlib
import logging
import threading
from collections import defaultdict, deque
from typing import Any, Callable, DefaultDict, cast

from hardware.exceptions import GpioConfigError, GpioConnectionError, GpioReadError, GpioWriteError
from hardware.gpio.types import (
    GpioBackend,
    GpioEdge,
    GpioExpanderSpec,
    GpioPinSpec,
    GpioSubscription,
    RawGpioCallback,
)

try:
    import adafruit_pcf8575  # type: ignore[import]
except (ModuleNotFoundError, RuntimeError):
    adafruit_pcf8575 = None  # type: ignore[assignment]

try:
    import board  # type: ignore[import]
except (ModuleNotFoundError, RuntimeError):
    board = None  # type: ignore[assignment]

try:
    import busio  # type: ignore[import]
except (ModuleNotFoundError, RuntimeError):
    busio = None  # type: ignore[assignment]

logger = logging.getLogger(__name__).getChild("pcf8575")

PcfDeviceFactory = Callable[[GpioExpanderSpec], Any]
I2cPort = tuple[Any, Any, Any]


def _available_i2c_ports() -> tuple[I2cPort, ...]:
    try:
        pin_module = importlib.import_module("microcontroller.pin")
    except (ModuleNotFoundError, RuntimeError):
        return ()

    ports = getattr(pin_module, "i2cPorts", ())

    return tuple(ports or ())


def _i2c_bus_id(port: I2cPort) -> int | None:
    try:
        return int(port[0])
    except (TypeError, ValueError):
        return None


class Pcf8575Backend:
    def __init__(
        self,
        expander: GpioExpanderSpec,
        *,
        rpi_backend: GpioBackend,
        device_factory: PcfDeviceFactory | None = None,
    ) -> None:
        self._expander = expander
        self._rpi_backend = rpi_backend
        self._device_factory = device_factory
        self._lock = threading.RLock()
        self._pcf: Any | None = None
        self._i2c: Any | None = None
        self._port_state: int | None = None
        self._write_latch = 0xFFFF
        self._interrupt_subscription: GpioSubscription | None = None
        self._callbacks: DefaultDict[int, list[RawGpioCallback]] = defaultdict(list)
        self._callback_dispatch_lock = threading.Lock()
        self._pending_callback_batches: deque[list[tuple[list[RawGpioCallback], bool]]] = deque()
        self._dispatching_callbacks = False
        self._closed = False

    def setup_input(self, spec: GpioPinSpec) -> None:
        self._validate_spec(spec)

        with self._lock:
            self._require_open_locked()
            self._ensure_device()
            try:
                # PCF8575 pins are quasi-bidirectional. Writing HIGH releases
                # the pin so external hardware can drive it as an input.
                self._set_latch_bit_locked(spec.pin, True)
                self._write_latch_locked()
                _, should_dispatch_callbacks = self._read_port_state_and_collect_callbacks_locked()
            except Exception as exc:
                raise GpioConnectionError(f"Failed to setup PCF8575 input '{spec.name}'") from exc

        if should_dispatch_callbacks:
            self._dispatch_pending_callbacks()

    def setup_output(self, spec: GpioPinSpec) -> None:
        self._validate_spec(spec)

        with self._lock:
            self._require_open_locked()
            self._ensure_device()
            raw_initial = spec.to_raw(spec.initial) if spec.initial is not None else False

            try:
                self._set_latch_bit_locked(spec.pin, raw_initial)
                self._write_latch_locked()
                self._set_cached_bit_locked(spec.pin, raw_initial)
            except Exception as exc:
                raise GpioConnectionError(f"Failed to setup PCF8575 output '{spec.name}'") from exc

    def read(self, spec: GpioPinSpec) -> bool:
        self._validate_spec(spec)

        with self._lock:
            self._require_open_locked()
            try:
                state, should_dispatch_callbacks = self._read_port_state_and_collect_callbacks_locked()
            except Exception as exc:
                raise GpioReadError(f"Failed to read PCF8575 pin '{spec.name}'") from exc

        if should_dispatch_callbacks:
            self._dispatch_pending_callbacks()
        return self._bit_is_high(state, spec.pin)

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None:
        self._validate_spec(spec)

        with self._lock:
            self._require_open_locked()
            self._ensure_device()

            try:
                self._set_latch_bit_locked(spec.pin, raw_value)
                self._write_latch_locked()
                self._set_cached_bit_locked(spec.pin, raw_value)
            except Exception as exc:
                raise GpioWriteError(f"Failed to write PCF8575 pin '{spec.name}'") from exc

    def subscribe(
        self,
        spec: GpioPinSpec,
        callback: RawGpioCallback,
        *,
        edge: GpioEdge = "both",
        debounce_ms: int = 0,
    ) -> GpioSubscription:
        del edge, debounce_ms
        self._validate_spec(spec)

        with self._lock:
            self._require_open_locked()
            self._callbacks[spec.pin].append(callback)
            try:
                should_dispatch_callbacks = self._ensure_interrupt_locked()
            except Exception:
                callbacks = self._callbacks[spec.pin]
                callbacks.pop()
                if not callbacks:
                    del self._callbacks[spec.pin]
                raise

        if should_dispatch_callbacks:
            self._dispatch_pending_callbacks()

        def did_unsubscribe() -> None:
            with self._lock:
                callbacks = self._callbacks.get(spec.pin)

                if callbacks is None:
                    return

                if callback in callbacks:
                    callbacks.remove(callback)

        return GpioSubscription(did_unsubscribe)

    def close(self) -> None:
        with self._lock:
            if self._closed is True:
                return

            self._closed = True
            interrupt_subscription = self._interrupt_subscription
            self._interrupt_subscription = None
            self._callbacks.clear()
            i2c = self._i2c
            self._i2c = None
            self._pcf = None
            self._port_state = None
            self._write_latch = 0xFFFF

        cleanup_error: Exception | None = None

        if interrupt_subscription is not None:
            try:
                interrupt_subscription.close()
            except Exception as exc:
                cleanup_error = exc

        if i2c is not None:
            deinit = cast(Callable[[], None] | None, getattr(i2c, "deinit", None))

            if deinit is not None:
                try:
                    deinit()
                except Exception:
                    logger.debug(
                        "Failed to close PCF8575 I2C bus",
                        extra={"expander": self._expander.name},
                    )

        if cleanup_error is not None:
            raise cleanup_error

    def _ensure_device(self):
        self._require_open_locked()

        if self._pcf is not None:
            return self._pcf

        if self._device_factory is not None:
            self._pcf = self._device_factory(self._expander)
            self._port_state = int(self._pcf.read_gpio())
            return self._pcf

        if adafruit_pcf8575 is None or busio is None:
            raise GpioConnectionError("adafruit-circuitpython-pcf8575 is not available")

        try:
            self._i2c = self._open_i2c_bus()
            self._pcf = adafruit_pcf8575.PCF8575(self._i2c, address=self._expander.address)
            self._port_state = int(self._pcf.read_gpio())
        except GpioConnectionError:
            self._release_i2c_after_connection_failure()
            self._pcf = None
            self._port_state = None
            raise
        except Exception as exc:
            self._release_i2c_after_connection_failure()
            self._pcf = None
            self._port_state = None
            raise GpioConnectionError(
                f"Failed to connect PCF8575 expander '{self._expander.name}' "
                f"on I2C bus {self._expander.bus} at address 0x{self._expander.address:02x}"
            ) from exc

        return self._pcf

    def _release_i2c_after_connection_failure(self) -> None:
        i2c = self._i2c
        self._i2c = None

        if i2c is None:
            return

        deinit = cast(Callable[[], None] | None, getattr(i2c, "deinit", None))

        if deinit is None:
            return

        try:
            deinit()
        except Exception:
            logger.debug(
                "Failed to clean up PCF8575 I2C bus after connection failure",
                extra={"expander": self._expander.name},
            )

    def _open_i2c_bus(self):
        port = self._configured_i2c_port()

        if port is not None:
            _, scl, sda = port
            return busio.I2C(scl, sda)

        if self._expander.bus == 1 and board is not None:
            return busio.I2C(board.SCL, board.SDA)

        valid_buses = [
            bus for bus in (_i2c_bus_id(port) for port in _available_i2c_ports()) if bus is not None
        ]
        valid_detail = ", ".join(str(bus) for bus in valid_buses) if valid_buses else "none detected"
        raise GpioConnectionError(
            f"I2C bus {self._expander.bus} is not available for PCF8575 expander "
            f"'{self._expander.name}' (valid buses: {valid_detail})"
        )

    def _configured_i2c_port(self) -> I2cPort | None:
        for port in _available_i2c_ports():
            if _i2c_bus_id(port) == self._expander.bus:
                return port

        return None

    def _ensure_interrupt_locked(self) -> bool:
        if self._interrupt_subscription is not None:
            return False

        interrupt = self._expander.interrupt

        if interrupt is None:
            raise GpioConfigError(
                f"PCF8575 expander '{self._expander.name}' has no interrupt pin configured"
            )

        interrupt_spec = GpioPinSpec(
            name=f"{self._expander.name}_interrupt",
            board="rpi",
            pin=interrupt.pin,
            direction="input",
            pull=interrupt.pull,
            debounce_ms=interrupt.debounce_ms,
        )

        previous_port_state = self._port_state
        interrupt_subscription: GpioSubscription | None = None

        try:
            self._rpi_backend.setup_input(interrupt_spec)
            interrupt_subscription = self._rpi_backend.subscribe(
                interrupt_spec,
                lambda _raw: self._did_interrupt(),
                edge=interrupt.edge,
                debounce_ms=interrupt.debounce_ms,
            )
            _, should_dispatch_callbacks = self._read_port_state_and_collect_callbacks_locked()
        except Exception:
            self._port_state = previous_port_state

            if interrupt_subscription is not None:
                try:
                    interrupt_subscription.close()
                except Exception:
                    logger.warning(
                        "Failed to roll back PCF8575 interrupt subscription",
                        extra={"expander": self._expander.name},
                        exc_info=True,
                    )
            raise

        self._interrupt_subscription = interrupt_subscription
        return should_dispatch_callbacks

    def _did_interrupt(self) -> None:
        with self._lock:
            if self._closed is True:
                return

            try:
                _, should_dispatch_callbacks = self._read_port_state_and_collect_callbacks_locked()
            except Exception:
                logger.warning(
                    "Failed to read PCF8575 after interrupt",
                    extra={"expander": self._expander.name},
                    exc_info=True,
                )
                return

        if should_dispatch_callbacks:
            self._dispatch_pending_callbacks()

    def _read_port_state_and_collect_callbacks_locked(self) -> tuple[int, bool]:
        previous = self._port_state
        pcf = self._ensure_device()
        current = int(pcf.read_gpio()) & 0xFFFF
        self._port_state = current

        if previous is None:
            return current, False

        changed = previous ^ current
        pending: list[tuple[list[RawGpioCallback], bool]] = []
        for pin, callbacks in self._callbacks.items():
            if changed & (1 << pin) == 0:
                continue

            pending.append((list(callbacks), self._bit_is_high(current, pin)))

        return current, self._queue_callbacks_locked(pending)

    def _queue_callbacks_locked(self, pending: list[tuple[list[RawGpioCallback], bool]]) -> bool:
        if not pending:
            return False

        with self._callback_dispatch_lock:
            self._pending_callback_batches.append(pending)
            if self._dispatching_callbacks:
                return False

            self._dispatching_callbacks = True
            return True

    def _dispatch_pending_callbacks(self) -> None:
        first_callback_error: BaseException | None = None

        while True:
            with self._callback_dispatch_lock:
                if not self._pending_callback_batches:
                    self._dispatching_callbacks = False
                    break

                pending = self._pending_callback_batches.popleft()

            callback_error = self._dispatch_callbacks(pending)
            if first_callback_error is None and callback_error is not None:
                first_callback_error = callback_error

        if first_callback_error is not None:
            raise first_callback_error

    @staticmethod
    def _dispatch_callbacks(pending: list[tuple[list[RawGpioCallback], bool]]) -> BaseException | None:
        first_callback_error: BaseException | None = None

        for callbacks, raw_value in pending:
            for callback in callbacks:
                try:
                    callback(raw_value)
                except BaseException as exc:
                    if first_callback_error is None:
                        first_callback_error = exc

        return first_callback_error

    def _write_latch_locked(self) -> None:
        pcf = self._ensure_device()
        pcf.write_gpio(self._write_latch & 0xFFFF)

    def _set_latch_bit_locked(self, pin: int, raw_value: bool) -> None:
        if raw_value is True:
            self._write_latch |= 1 << pin
        else:
            self._write_latch &= ~(1 << pin)

        self._write_latch &= 0xFFFF

    def _set_cached_bit_locked(self, pin: int, raw_value: bool) -> None:
        state = self._port_state

        if state is None:
            self._ensure_device()
            state = self._port_state
            assert state is not None
        if raw_value is True:
            self._port_state = state | (1 << pin)
        else:
            self._port_state = state & ~(1 << pin)

    def _validate_spec(self, spec: GpioPinSpec) -> None:
        if spec.board != "pcf8575":
            raise GpioConfigError(f"GPIO pin '{spec.name}' is not a PCF8575 pin")

        if spec.expander != self._expander.name:
            raise GpioConfigError(
                f"GPIO pin '{spec.name}' is assigned to unknown PCF8575 expander '{spec.expander}'"
            )
        if spec.pin < 0 or spec.pin > 15:
            raise GpioConfigError(f"PCF8575 pin '{spec.name}' must be in range 0..15")

    def _require_open_locked(self) -> None:
        if self._closed is True:
            raise GpioConfigError(f"PCF8575 backend '{self._expander.name}' is closed")

    @staticmethod
    def _bit_is_high(state: int, pin: int) -> bool:
        return bool(state & (1 << pin))
