from __future__ import annotations

import threading
from collections import defaultdict
from typing import DefaultDict

from hardware.exceptions import GpioConfigError
from hardware.gpio.types import GpioEdge, GpioPinSpec, GpioSubscription, RawGpioCallback


class FakeGpioBackend:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str | None, int], bool] = {}
        self._callbacks: DefaultDict[
            tuple[str, str | None, int], list[RawGpioCallback]
        ] = defaultdict(list)
        self._directions: dict[tuple[str, str | None, int], str] = {}
        self._lock = threading.RLock()

    def setup_input(self, spec: GpioPinSpec) -> None:
        with self._lock:
            self._directions[spec.physical_key] = "input"
            self._values.setdefault(spec.physical_key, True)

    def setup_output(self, spec: GpioPinSpec) -> None:
        with self._lock:
            self._directions[spec.physical_key] = "output"
            if spec.initial is not None:
                self._values[spec.physical_key] = spec.to_raw(spec.initial)
            else:
                self._values.setdefault(spec.physical_key, False)

    def read(self, spec: GpioPinSpec) -> bool:
        with self._lock:
            return self._values.get(spec.physical_key, True)

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None:
        with self._lock:
            if self._directions.get(spec.physical_key) != "output":
                raise GpioConfigError(f"GPIO pin '{spec.name}' is not configured as output")
            self._set_raw_locked(spec, raw_value)

    def subscribe(
        self,
        spec: GpioPinSpec,
        callback: RawGpioCallback,
        *,
        edge: GpioEdge = "both",
        debounce_ms: int = 0,
    ) -> GpioSubscription:
        del edge, debounce_ms

        with self._lock:
            self._callbacks[spec.physical_key].append(callback)

        def did_unsubscribe() -> None:
            with self._lock:
                callbacks = self._callbacks.get(spec.physical_key)
                if callbacks is None:
                    return
                if callback in callbacks:
                    callbacks.remove(callback)

        return GpioSubscription(did_unsubscribe)

    def set_raw(self, spec: GpioPinSpec, raw_value: bool) -> None:
        with self._lock:
            self._set_raw_locked(spec, raw_value)

    def _set_raw_locked(self, spec: GpioPinSpec, raw_value: bool) -> None:
        previous = self._values.get(spec.physical_key)
        self._values[spec.physical_key] = raw_value

        if previous == raw_value:
            return

        callbacks = list(self._callbacks.get(spec.physical_key, []))
        for callback in callbacks:
            callback(raw_value)

    def close(self) -> None:
        with self._lock:
            self._callbacks.clear()
