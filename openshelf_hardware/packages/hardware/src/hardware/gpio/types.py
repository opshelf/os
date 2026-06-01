from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Literal, Optional, Protocol

from hardware.exceptions import GpioConfigError

logger = logging.getLogger(__name__).getChild("gpio")

GpioBoard = Literal["rpi", "pcf8575"]
GpioDirection = Literal["input", "output"]
GpioEdge = Literal["rising", "falling", "both"]
GpioPull = Literal["up", "down", "off"]
GpioCallback = Callable[["GpioEvent"], None]
RawGpioCallback = Callable[[bool], None]


@dataclass(frozen=True)
class GpioEvent:
    name: str
    value: bool
    raw_value: bool
    timestamp: float


class GpioSubscription:
    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe
        self._closed = False

    def close(self) -> None:
        if self._closed is True:
            return

        self._closed = True
        self._unsubscribe()


@dataclass(frozen=True)
class GpioInterruptSpec:
    board: GpioBoard
    pin: int
    pull: GpioPull = "up"
    edge: GpioEdge = "falling"
    debounce_ms: int = 0

    def __post_init__(self) -> None:
        if self.board != "rpi":
            raise GpioConfigError("PCF8575 interrupt pins must use the rpi board")
        if self.pin < 0:
            raise GpioConfigError("GPIO interrupt pin must be >= 0")
        if self.debounce_ms < 0:
            raise GpioConfigError("GPIO interrupt debounce_ms must be >= 0")


@dataclass(frozen=True)
class GpioExpanderSpec:
    name: str
    kind: Literal["pcf8575"]
    bus: int
    address: int
    interrupt: Optional[GpioInterruptSpec] = None

    def __post_init__(self) -> None:
        if not self.name:
            raise GpioConfigError("GPIO expander name is required")
        if self.bus < 0:
            raise GpioConfigError("GPIO expander bus must be >= 0")
        if self.address < 0 or self.address > 0x7F:
            raise GpioConfigError("GPIO expander address must be in range 0..127")


@dataclass(frozen=True)
class GpioPinSpec:
    name: str
    board: GpioBoard
    pin: int
    direction: GpioDirection
    expander: Optional[str] = None
    pull: GpioPull = "off"
    inverted: bool = False
    debounce_ms: int = 0
    initial: Optional[bool] = None
    shutdown: Optional[bool] = None
    required: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise GpioConfigError("GPIO pin name is required")
        if self.pin < 0:
            raise GpioConfigError(f"GPIO pin '{self.name}' must be >= 0")
        if self.debounce_ms < 0:
            raise GpioConfigError(f"GPIO pin '{self.name}' debounce_ms must be >= 0")
        if self.board == "pcf8575" and not self.expander:
            raise GpioConfigError(f"GPIO pin '{self.name}' requires an expander")
        if self.board == "rpi" and self.expander is not None:
            raise GpioConfigError(f"GPIO pin '{self.name}' cannot set expander for rpi board")
        if self.direction == "input" and (self.initial is not None or self.shutdown is not None):
            raise GpioConfigError(f"GPIO input '{self.name}' cannot set initial or shutdown")

    @property
    def physical_key(self) -> tuple[str, str | None, int]:
        return self.board, self.expander, self.pin

    def to_logical(self, raw_value: bool) -> bool:
        return not raw_value if self.inverted is True else raw_value

    def to_raw(self, value: bool) -> bool:
        return not value if self.inverted is True else value


class GpioBackend(Protocol):
    def setup_input(self, spec: GpioPinSpec) -> None: ...

    def setup_output(self, spec: GpioPinSpec) -> None: ...

    def read(self, spec: GpioPinSpec) -> bool: ...

    def write(self, spec: GpioPinSpec, raw_value: bool) -> None: ...

    def subscribe(
        self,
        spec: GpioPinSpec,
        callback: RawGpioCallback,
        *,
        edge: GpioEdge = "both",
        debounce_ms: int = 0,
    ) -> GpioSubscription: ...

    def close(self) -> None: ...


class DigitalInput:
    def __init__(self, spec: GpioPinSpec, backend: GpioBackend) -> None:
        self._spec = spec
        self._backend = backend

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def pin(self) -> int:
        return self._spec.pin

    @property
    def board(self) -> str:
        return self._spec.board

    def read(self) -> bool:
        raw_value = self._backend.read(self._spec)
        return self._spec.to_logical(raw_value)

    def subscribe(self, callback: GpioCallback, *, emit_initial: bool = False) -> GpioSubscription:
        loop = self._get_running_loop()
        raw_initial = self._backend.read(self._spec)
        last_value = self._spec.to_logical(raw_initial)
        last_emit_at = 0.0
        is_closed = False

        def did_call(event: GpioEvent) -> None:
            try:
                callback(event)
            except Exception:
                logger.warning(
                    "GPIO subscriber failed",
                    extra={"pin": self._spec.name},
                    exc_info=True,
                )

        def did_dispatch(event: GpioEvent) -> None:
            if loop is not None and loop.is_closed() is False:
                loop.call_soon_threadsafe(did_call, event)
                return

            did_call(event)

        def did_change(raw_value: bool) -> None:
            nonlocal last_emit_at, last_value

            if is_closed is True:
                return

            value = self._spec.to_logical(raw_value)
            if value == last_value:
                return

            now = time.monotonic()
            if self._spec.debounce_ms > 0:
                debounce_s = self._spec.debounce_ms / 1000
                if last_emit_at > 0 and now - last_emit_at < debounce_s:
                    return

            last_value = value
            last_emit_at = now
            did_dispatch(
                GpioEvent(
                    name=self._spec.name,
                    value=value,
                    raw_value=raw_value,
                    timestamp=now,
                )
            )

        subscription = self._backend.subscribe(
            self._spec,
            did_change,
            edge="both",
            debounce_ms=self._spec.debounce_ms,
        )

        if emit_initial is True:
            last_emit_at = time.monotonic()
            did_dispatch(
                GpioEvent(
                    name=self._spec.name,
                    value=last_value,
                    raw_value=raw_initial,
                    timestamp=last_emit_at,
                )
            )

        def did_unsubscribe() -> None:
            nonlocal is_closed
            is_closed = True
            subscription.close()

        return GpioSubscription(did_unsubscribe)

    @staticmethod
    def _get_running_loop() -> asyncio.AbstractEventLoop | None:
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None


class DigitalOutput:
    def __init__(self, spec: GpioPinSpec, backend: GpioBackend) -> None:
        self._spec = spec
        self._backend = backend

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def pin(self) -> int:
        return self._spec.pin

    @property
    def board(self) -> str:
        return self._spec.board

    def read(self) -> bool:
        raw_value = self._backend.read(self._spec)
        return self._spec.to_logical(raw_value)

    def write(self, value: bool) -> None:
        self._backend.write(self._spec, self._spec.to_raw(value))

    def shutdown(self) -> None:
        if self._spec.shutdown is None:
            return

        self.write(self._spec.shutdown)
