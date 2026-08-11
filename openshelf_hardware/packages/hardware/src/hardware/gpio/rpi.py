from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Callable, DefaultDict, cast

from hardware.exceptions import GpioConfigError, GpioConnectionError, GpioReadError, GpioWriteError
from hardware.gpio.types import GpioEdge, GpioPinSpec, GpioPull, GpioSubscription, RawGpioCallback

try:
    import RPi.GPIO as GPIO  # type: ignore[import]
except (ModuleNotFoundError, RuntimeError):
    GPIO = None  # type: ignore[assignment]

logger = logging.getLogger(__name__).getChild("rpi")


class RpiGpioBackend:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._configured: set[int] = set()
        self._configured_specs: dict[int, GpioPinSpec] = {}
        self._output_values: dict[int, bool] = {}
        self._event_pins: set[int] = set()
        self._event_configs: dict[int, tuple[GpioEdge, int]] = {}
        self._callbacks: DefaultDict[int, list[RawGpioCallback]] = defaultdict(list)
        self._require_gpio()

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

    def _require_gpio(self):
        if GPIO is None:
            raise GpioConnectionError("RPi.GPIO not available")

        return GPIO

    def setup_input(self, spec: GpioPinSpec) -> None:
        with self._lock:
            previous = self._configured_specs.get(spec.pin)
            if spec.pin in self._event_pins:
                if (
                    previous is not None
                    and previous.direction == "input"
                    and previous.pull == spec.pull
                ):
                    # Shared expander interrupt lines legitimately request the
                    # same physical input more than once. In rpi-lgpio,
                    # GPIO.setup() frees and reclaims an existing line, which
                    # silently removes its kernel alert. Preserve the active
                    # edge detector when the electrical setup is unchanged.
                    return

                raise GpioConfigError(
                    f"RPi GPIO pin {spec.pin} cannot be reconfigured while edge detection is active"
                )

            self._setup_input_locked(spec)

    def setup_output(self, spec: GpioPinSpec) -> None:
        with self._lock:
            self._setup_output_locked(spec)

    def read(self, spec: GpioPinSpec) -> bool:
        gpio = self._require_gpio()

        with self._lock:
            try:
                return bool(gpio.input(spec.pin) == gpio.HIGH)
            except Exception as exc:
                if self._is_unconfigured_channel_error(exc) is False:
                    raise GpioReadError(f"Failed to read GPIO pin '{spec.name}'") from exc

                try:
                    self._recover_channel_locked(spec)
                    return bool(gpio.input(spec.pin) == gpio.HIGH)
                except Exception as retry_exc:
                    raise GpioReadError(f"Failed to read GPIO pin '{spec.name}'") from retry_exc

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None:
        gpio = self._require_gpio()

        with self._lock:
            try:
                gpio.output(spec.pin, gpio.HIGH if raw_value else gpio.LOW)
                self._output_values[spec.pin] = raw_value
            except Exception as exc:
                if self._is_unconfigured_channel_error(exc) is False:
                    raise GpioWriteError(f"Failed to write GPIO pin '{spec.name}'") from exc

                try:
                    self._recover_channel_locked(spec)
                    gpio.output(spec.pin, gpio.HIGH if raw_value else gpio.LOW)
                    self._output_values[spec.pin] = raw_value
                except Exception as retry_exc:
                    raise GpioWriteError(f"Failed to write GPIO pin '{spec.name}'") from retry_exc

    def _setup_input_locked(self, spec: GpioPinSpec) -> None:
        gpio = self._require_gpio()
        gpio.setup(spec.pin, gpio.IN, pull_up_down=self._pull_constant(spec.pull))
        self._configured.add(spec.pin)
        self._configured_specs[spec.pin] = spec

    def _setup_output_locked(self, spec: GpioPinSpec) -> None:
        gpio = self._require_gpio()
        raw_initial = self._output_values.get(spec.pin)
        if raw_initial is None and spec.initial is not None:
            raw_initial = spec.to_raw(spec.initial)

        if raw_initial is None:
            gpio.setup(spec.pin, gpio.OUT)
        else:
            gpio.setup(spec.pin, gpio.OUT, initial=gpio.HIGH if raw_initial else gpio.LOW)
            self._output_values[spec.pin] = raw_initial

        self._configured.add(spec.pin)
        self._configured_specs[spec.pin] = spec

    def _recover_channel_locked(self, spec: GpioPinSpec) -> None:
        logger.warning(
            "RPi GPIO channel lost setup; reconfiguring",
            extra={"pin": spec.pin, "pin_name": spec.name},
        )
        self._configured.discard(spec.pin)
        self._configured_specs.pop(spec.pin, None)
        self._setup_for_spec_locked(spec)
        self._restore_event_detect_locked(spec.pin)

    def _setup_for_spec_locked(self, spec: GpioPinSpec) -> None:
        if spec.direction == "input":
            self._setup_input_locked(spec)
            return

        if spec.direction == "output":
            self._setup_output_locked(spec)
            return

        raise GpioConfigError(f"Unsupported GPIO direction '{spec.direction}' for pin '{spec.name}'")

    def _restore_event_detect_locked(self, pin: int) -> None:
        event_config = self._event_configs.get(pin)
        if event_config is None or pin not in self._event_pins:
            return

        edge, debounce_ms = event_config
        try:
            self._add_event_detect_locked(pin, edge=edge, debounce_ms=debounce_ms)
        except Exception:
            logger.warning(
                "Failed to restore RPi GPIO event detect after reconfigure",
                extra={"pin": pin},
                exc_info=True,
            )

    def _add_event_detect_locked(self, pin: int, *, edge: GpioEdge, debounce_ms: int) -> None:
        gpio = self._require_gpio()
        gpio.add_event_detect(
            pin,
            self._edge_constant(edge),
            callback=self._did_detect_edge,
            bouncetime=debounce_ms if debounce_ms > 0 else None,
        )

    @staticmethod
    def _is_unconfigured_channel_error(exc: Exception) -> bool:
        if isinstance(exc, RuntimeError) is False:
            return False

        message = str(exc).lower()
        return "setup" in message and ("channel" in message or "pin" in message)

    def subscribe(
        self,
        spec: GpioPinSpec,
        callback: RawGpioCallback,
        *,
        edge: GpioEdge = "both",
        debounce_ms: int = 0,
    ) -> GpioSubscription:
        self._require_gpio()

        with self._lock:
            self._callbacks[spec.pin].append(callback)

            if spec.pin not in self._event_pins:
                self._add_event_detect_locked(spec.pin, edge=edge, debounce_ms=debounce_ms)
                self._event_pins.add(spec.pin)
                self._event_configs[spec.pin] = (edge, debounce_ms)

        def did_unsubscribe() -> None:
            with self._lock:
                callbacks = self._callbacks.get(spec.pin)

                if callbacks is None:
                    return
                if callback in callbacks:
                    callbacks.remove(callback)

        return GpioSubscription(did_unsubscribe)

    def _did_detect_edge(self, pin: int) -> None:
        gpio = self._require_gpio()

        with self._lock:
            try:
                raw_value = bool(gpio.input(pin) == gpio.HIGH)
            except Exception as exc:
                spec = self._configured_specs.get(pin)
                if spec is None or self._is_unconfigured_channel_error(exc) is False:
                    logger.warning("Failed to read RPi GPIO edge", extra={"pin": pin}, exc_info=True)
                    return

                try:
                    self._recover_channel_locked(spec)
                    raw_value = bool(gpio.input(pin) == gpio.HIGH)
                except Exception:
                    logger.warning("Failed to read RPi GPIO edge", extra={"pin": pin}, exc_info=True)
                    return

            callbacks = list(self._callbacks.get(pin, []))

        for callback in callbacks:
            callback(raw_value)

    def close(self) -> None:
        gpio = self._require_gpio()

        with self._lock:
            for pin in list(self._event_pins):
                remove_event_detect = cast(
                    Callable[[int], None] | None,
                    getattr(gpio, "remove_event_detect", None),
                )

                if remove_event_detect is not None:
                    try:
                        remove_event_detect(pin)
                    except Exception:
                        logger.debug("Failed to remove RPi GPIO event detect", extra={"pin": pin})

            for pin in list(self._configured):
                try:
                    gpio.cleanup(pin)
                except Exception:
                    logger.debug("Failed to cleanup RPi GPIO", extra={"pin": pin})

            self._callbacks.clear()
            self._event_pins.clear()
            self._event_configs.clear()
            self._configured.clear()
            self._configured_specs.clear()
            self._output_values.clear()

    def _pull_constant(self, pull: GpioPull) -> int:
        gpio = self._require_gpio()

        if pull == "up":
            return gpio.PUD_UP

        if pull == "down":
            return gpio.PUD_DOWN

        if pull == "off":
            return gpio.PUD_OFF

        raise GpioConfigError(f"Unsupported GPIO pull value: {pull}")

    def _edge_constant(self, edge: GpioEdge) -> int:
        gpio = self._require_gpio()

        if edge == "rising":
            return gpio.RISING

        if edge == "falling":
            return gpio.FALLING

        if edge == "both":
            return gpio.BOTH

        raise GpioConfigError(f"Unsupported GPIO edge value: {edge}")
