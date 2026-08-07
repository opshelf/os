from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable, DefaultDict

from hardware.exceptions import GpioConfigError, GpioConnectionError, GpioReadError, GpioWriteError
from hardware.gpio.pca9535_mod import PCA9535
from hardware.gpio.types import (
    GpioBackend,
    GpioEdge,
    GpioExpanderSpec,
    GpioPinSpec,
    GpioSubscription,
    RawGpioCallback,
)

logger = logging.getLogger(__name__).getChild("pca9535")

Pca9535DeviceFactory = Callable[[GpioExpanderSpec], Any]


class Pca9535Backend:
    def __init__(
        self,
        expander: GpioExpanderSpec,
        *,
        rpi_backend: GpioBackend,
        device_factory: Pca9535DeviceFactory | None = None,
    ) -> None:
        self._expander = expander
        self._rpi_backend = rpi_backend
        self._device_factory = device_factory
        self._lock = threading.RLock()
        self._device: Any | None = None
        self._port_state: int | None = None
        self._interrupt_subscription: GpioSubscription | None = None
        self._callbacks: DefaultDict[int, list[RawGpioCallback]] = defaultdict(list)

    def setup_input(self, spec: GpioPinSpec) -> None:
        self._validate_spec(spec)

        with self._lock:
            device = self._ensure_device()
            try:
                device.pin_config_write(spec.pin, 1)
            except Exception as exc:
                raise GpioConnectionError(f"Failed to setup PCA9535 input '{spec.name}'") from exc

    def setup_output(self, spec: GpioPinSpec) -> None:
        self._validate_spec(spec)

        with self._lock:
            device = self._ensure_device()
            raw_initial = spec.to_raw(spec.initial) if spec.initial is not None else False

            try:
                device.pin_output_write(spec.pin, 1 if raw_initial else 0)
                device.pin_config_write(spec.pin, 0)
            except Exception as exc:
                raise GpioConnectionError(f"Failed to setup PCA9535 output '{spec.name}'") from exc

    def read(self, spec: GpioPinSpec) -> bool:
        self._validate_spec(spec)

        with self._lock:
            device = self._ensure_device()
            try:
                return bool(device.pin_input_read(spec.pin))
            except Exception as exc:
                raise GpioReadError(f"Failed to read PCA9535 pin '{spec.name}'") from exc

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None:
        self._validate_spec(spec)

        with self._lock:
            device = self._ensure_device()
            try:
                device.pin_output_write(spec.pin, 1 if raw_value else 0)
            except Exception as exc:
                raise GpioWriteError(f"Failed to write PCA9535 pin '{spec.name}'") from exc

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
            self._ensure_interrupt_locked()
            self._callbacks[spec.pin].append(callback)

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
            if self._interrupt_subscription is not None:
                self._interrupt_subscription.close()
                self._interrupt_subscription = None

            self._callbacks.clear()
            self._device = None
            self._port_state = None

    def _ensure_device(self):
        if self._device is not None:
            return self._device

        if self._device_factory is not None:
            self._device = self._device_factory(self._expander)
            return self._device

        try:
            device = PCA9535(bus_num=self._expander.bus, address=self._expander.address)
            # The Polarity Inversion registers are chip state that persists across
            # process restarts (and possibly across firmware/test tooling that ran
            # before this driver ever touched the chip). Force them to non-inverting
            # here so GpioPinSpec.inverted is the single source of truth for polarity,
            # rather than trusting whatever REG_INVERSION happened to be left at.
            device.port_invert_write(0x00, 0x00)
        except Exception as exc:
            self._device = None
            raise GpioConnectionError(
                f"Failed to connect PCA9535 expander '{self._expander.name}' "
                f"on I2C bus {self._expander.bus} at address 0x{self._expander.address:02x}"
            ) from exc

        self._device = device
        return self._device

    def _ensure_interrupt_locked(self) -> None:
        if self._interrupt_subscription is not None:
            return

        interrupt = self._expander.interrupt

        if interrupt is None:
            raise GpioConfigError(
                f"PCA9535 expander '{self._expander.name}' has no interrupt pin configured"
            )

        interrupt_spec = GpioPinSpec(
            name=f"{self._expander.name}_interrupt",
            board="rpi",
            pin=interrupt.pin,
            direction="input",
            pull=interrupt.pull,
            debounce_ms=interrupt.debounce_ms,
        )

        self._rpi_backend.setup_input(interrupt_spec)
        self._refresh_port_state_locked()
        self._interrupt_subscription = self._rpi_backend.subscribe(
            interrupt_spec,
            lambda _raw: self._did_interrupt(),
            edge=interrupt.edge,
            debounce_ms=interrupt.debounce_ms,
        )

    def _did_interrupt(self) -> None:
        with self._lock:
            previous = self._port_state

            try:
                current = self._refresh_port_state_locked()
            except Exception:
                logger.warning(
                    "Failed to read PCA9535 after interrupt",
                    extra={"expander": self._expander.name},
                    exc_info=True,
                )
                return

            if previous is None:
                return

            changed = previous ^ current
            pending: list[tuple[list[RawGpioCallback], bool]] = []
            for pin, callbacks in self._callbacks.items():
                if changed & (1 << pin) == 0:
                    continue

                pending.append((list(callbacks), self._bit_is_high(current, pin)))

        for callbacks, raw_value in pending:
            for callback in callbacks:
                callback(raw_value)

    def _refresh_port_state_locked(self) -> int:
        device = self._ensure_device()
        port0, port1 = device.port_input_read()
        state = (port0 & 0xFF) | ((port1 & 0xFF) << 8)
        self._port_state = state

        return state

    def _validate_spec(self, spec: GpioPinSpec) -> None:
        if spec.board != "pca9535":
            raise GpioConfigError(f"GPIO pin '{spec.name}' is not a PCA9535 pin")

        if spec.expander != self._expander.name:
            raise GpioConfigError(
                f"GPIO pin '{spec.name}' is assigned to unknown PCA9535 expander '{spec.expander}'"
            )
        if spec.pin < 0 or spec.pin > 15:
            raise GpioConfigError(f"PCA9535 pin '{spec.name}' must be in range 0..15")

    @staticmethod
    def _bit_is_high(state: int, pin: int) -> bool:
        return bool(state & (1 << pin))
