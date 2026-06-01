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
        self._event_pins: set[int] = set()
        self._callbacks: DefaultDict[int, list[RawGpioCallback]] = defaultdict(list)
        self._require_gpio()
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

    def _require_gpio(self):
        if GPIO is None:
            raise GpioConnectionError("RPi.GPIO not available")
        return GPIO

    def setup_input(self, spec: GpioPinSpec) -> None:
        gpio = self._require_gpio()
        with self._lock:
            gpio.setup(spec.pin, gpio.IN, pull_up_down=self._pull_constant(spec.pull))
            self._configured.add(spec.pin)

    def setup_output(self, spec: GpioPinSpec) -> None:
        gpio = self._require_gpio()
        with self._lock:
            if spec.initial is None:
                gpio.setup(spec.pin, gpio.OUT)
            else:
                gpio.setup(spec.pin, gpio.OUT, initial=gpio.HIGH if spec.to_raw(spec.initial) else gpio.LOW)
            self._configured.add(spec.pin)

    def read(self, spec: GpioPinSpec) -> bool:
        gpio = self._require_gpio()
        try:
            return bool(gpio.input(spec.pin) == gpio.HIGH)
        except Exception as exc:
            raise GpioReadError(f"Failed to read GPIO pin '{spec.name}'") from exc

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None:
        gpio = self._require_gpio()
        try:
            gpio.output(spec.pin, gpio.HIGH if raw_value else gpio.LOW)
        except Exception as exc:
            raise GpioWriteError(f"Failed to write GPIO pin '{spec.name}'") from exc

    def subscribe(
        self,
        spec: GpioPinSpec,
        callback: RawGpioCallback,
        *,
        edge: GpioEdge = "both",
        debounce_ms: int = 0,
    ) -> GpioSubscription:
        gpio = self._require_gpio()

        with self._lock:
            self._callbacks[spec.pin].append(callback)
            if spec.pin not in self._event_pins:
                gpio.add_event_detect(
                    spec.pin,
                    self._edge_constant(edge),
                    callback=self._did_detect_edge,
                    bouncetime=debounce_ms if debounce_ms > 0 else None,
                )
                self._event_pins.add(spec.pin)

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
        try:
            raw_value = bool(gpio.input(pin) == gpio.HIGH)
        except Exception:
            logger.warning("Failed to read RPi GPIO edge", extra={"pin": pin}, exc_info=True)
            return

        with self._lock:
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
            self._configured.clear()

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
