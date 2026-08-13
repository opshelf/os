from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, cast

from hardware.exceptions import GpioConfigError
from hardware.gpio.fake import FakeGpioBackend
from hardware.gpio.pca9535 import Pca9535Backend
from hardware.gpio.pcf8575 import Pcf8575Backend
from hardware.gpio.rpi import RpiGpioBackend
from hardware.gpio.types import (
    DigitalInput,
    DigitalOutput,
    GpioBackend,
    GpioEvent,
    GpioExpanderSpec,
    GpioInterruptSpec,
    GpioPinSpec,
    GpioSubscription,
)

logger = logging.getLogger(__name__)


class GpioManager:
    def __init__(
        self,
        pins: Mapping[str, GpioPinSpec],
        expanders: Mapping[str, GpioExpanderSpec] | None = None,
        *,
        fake: bool = False,
        backends: Mapping[str, GpioBackend] | None = None,
    ) -> None:
        self._pins = dict(pins)
        self._expanders = dict(expanders or {})
        self._fake = fake
        self._injected_backends = dict(backends or {})
        self._created_backends: dict[str, GpioBackend] = {}
        self._inputs: dict[str, DigitalInput] = {}
        self._outputs: dict[str, DigitalOutput] = {}
        self._input_log_subscriptions: list[GpioSubscription] = []
        self._closed = False
        self._validate_unique_physical_pins()

    @classmethod
    def from_config(cls, config: Any | None, *, fake: bool = False) -> "GpioManager":
        data = _to_dict(config)

        if data is None:
            data = {}

        expanders: dict[str, GpioExpanderSpec] = {}
        for expander_data in data.get("expanders", []) or []:
            expander = _parse_expander(expander_data)
            expanders[expander.name] = expander

        pins: dict[str, GpioPinSpec] = {}
        for name, pin_data in (data.get("pins", {}) or {}).items():
            pins[name] = _parse_pin(name, pin_data)

        return cls(pins, expanders, fake=fake)


    def has_pin(self, name: str) -> bool:
        return name in self._pins

    def input(self, name: str) -> DigitalInput:
        if self._closed is True:
            raise GpioConfigError("GPIO manager is closed")

        spec = self._require_pin(name)

        if spec.direction != "input":
            raise GpioConfigError(f"GPIO pin '{name}' is not configured as input")

        if name not in self._inputs:
            backend = self._backend_for(spec)
            backend.setup_input(spec)
            digital_input = DigitalInput(spec, backend)
            self._inputs[name] = digital_input
            try:
                subscription = digital_input.subscribe(_log_input_change)
            except Exception:
                logger.warning(
                    "Failed to subscribe to GPIO input '%s' for diagnostic logging",
                    name,
                    exc_info=True,
                )
            else:
                self._input_log_subscriptions.append(subscription)

        return self._inputs[name]

    def output(self, name: str) -> DigitalOutput:
        if self._closed is True:
            raise GpioConfigError("GPIO manager is closed")

        spec = self._require_pin(name)

        if spec.direction != "output":
            raise GpioConfigError(f"GPIO pin '{name}' is not configured as output")

        if name not in self._outputs:
            backend = self._backend_for(spec)
            backend.setup_output(spec)
            self._outputs[name] = DigitalOutput(spec, backend)

        return self._outputs[name]

    def apply_initial_outputs(self) -> None:
        for name, spec in self._pins.items():
            if spec.direction != "output" or spec.initial is None:
                continue

            self.output(name)

    def observe_all_inputs(self) -> None:
        for name, spec in self._pins.items():
            if spec.direction != "input":
                continue

            self.input(name)

    def set_fake_input(self, name: str, value: bool, *, raw: bool = False) -> None:
        spec = self._require_pin(name)

        if self._fake is False:
            raise GpioConfigError("set_fake_input is only available on fake GPIO managers")

        backend = self._backend_for(spec)

        if not isinstance(backend, FakeGpioBackend):
            raise GpioConfigError("GPIO manager is not using the fake backend")

        backend.set_raw(spec, value if raw is True else spec.to_raw(value))

    def close(self) -> None:
        if self._closed is True:
            return

        errors = []
        for output in list(self._outputs.values()):
            try:
                output.shutdown()
            except Exception as exc:
                errors.append(exc)

        for subscription in self._input_log_subscriptions:
            subscription.close()
        self._input_log_subscriptions.clear()

        for backend in self._unique_backends():
            try:
                backend.close()
            except Exception as exc:
                errors.append(exc)

        self._closed = True

        if errors:
            raise GpioConfigError("GPIO close failed") from errors[0]

    def _require_pin(self, name: str) -> GpioPinSpec:
        try:
            return self._pins[name]
        except KeyError as exc:
            raise GpioConfigError(f"GPIO pin '{name}' is not configured") from exc

    def _backend_for(self, spec: GpioPinSpec) -> GpioBackend:
        if spec.board in ("pca9535", "pcf8575"):
            if self._fake is True:
                self._validate_fake_expander(spec)
                return self._get_or_create_backend("fake", lambda: FakeGpioBackend())

            expander = self._expander_for(spec)

            if expander.kind == "pcf8575":
                return self._get_or_create_backend(
                    f"pcf8575:{spec.expander}",
                    lambda: Pcf8575Backend(expander, rpi_backend=self._rpi_backend()),
                )

            return self._get_or_create_backend(
                f"pca9535:{spec.expander}",
                lambda: Pca9535Backend(expander, rpi_backend=self._rpi_backend()),
            )

        if self._fake is True:
            return self._get_or_create_backend("fake", lambda: FakeGpioBackend())

        if spec.board == "rpi":
            return self._get_or_create_backend("rpi", lambda: RpiGpioBackend())

        raise GpioConfigError(f"Unsupported GPIO board '{spec.board}'")

    def _validate_fake_expander(self, spec: GpioPinSpec) -> None:
        if spec.expander is None:
            raise GpioConfigError(f"GPIO pin '{spec.name}' requires an expander")

        expander = self._expanders.get(spec.expander)

        if expander is not None and expander.kind != spec.board:
            raise GpioConfigError(
                f"GPIO pin '{spec.name}' uses board '{spec.board}' but expander "
                f"'{spec.expander}' has type '{expander.kind}'"
            )

    def _expander_for(self, spec: GpioPinSpec) -> GpioExpanderSpec:
        if spec.expander is None:
            raise GpioConfigError(f"GPIO pin '{spec.name}' requires an expander")

        expander = self._expanders.get(spec.expander)

        if expander is None:
            raise GpioConfigError(
                f"GPIO pin '{spec.name}' references unknown expander '{spec.expander}'"
            )

        if expander.kind != spec.board:
            raise GpioConfigError(
                f"GPIO pin '{spec.name}' uses board '{spec.board}' but expander "
                f"'{spec.expander}' has type '{expander.kind}'"
            )

        return expander

    def _rpi_backend(self) -> GpioBackend:
        return self._get_or_create_backend("rpi", lambda: RpiGpioBackend())

    def _get_or_create_backend(self, key: str, factory: Callable[[], GpioBackend]) -> GpioBackend:
        if key in self._injected_backends:
            return self._injected_backends[key]

        if key not in self._created_backends:
            self._created_backends[key] = factory()

        return self._created_backends[key]

    def _unique_backends(self) -> list[GpioBackend]:
        seen: set[int] = set()
        result: list[GpioBackend] = []

        for backend in [*self._created_backends.values(), *self._injected_backends.values()]:
            identity = id(backend)

            if identity in seen:
                continue

            seen.add(identity)
            result.append(backend)

        return result

    def _validate_unique_physical_pins(self) -> None:
        claimed: dict[tuple[str, str | None, int], str] = {}

        for name, spec in self._pins.items():
            previous = claimed.get(spec.physical_key)

            if previous is not None:
                raise GpioConfigError(
                    f"GPIO pins '{previous}' and '{name}' share physical pin {spec.physical_key}"
                )

            claimed[spec.physical_key] = name

        for expander in self._expanders.values():
            interrupt = expander.interrupt

            if interrupt is None:
                continue

            key = (interrupt.board, None, interrupt.pin)
            previous = claimed.get(key)

            # Multiple expanders may legitimately share one interrupt pin (a
            # wired-AND INT line tied to several chips), so only a collision
            # with a directly-claimed rpi pin is treated as an error - other
            # expanders' interrupts are deliberately not recorded in `claimed`.
            if previous is not None:
                raise GpioConfigError(
                    f"GPIO expander '{expander.name}' interrupt shares physical pin with '{previous}'"
                )


def _log_input_change(event: GpioEvent) -> None:
    logger.info("%s: %s", event.name, "HIGH" if event.value else "LOW")


def _to_dict(value: Any | None) -> dict[str, Any] | None:
    if value is None:
        return None

    if isinstance(value, Mapping):
        return dict(value)

    model_dump = cast(Callable[[], dict[str, Any]] | None, getattr(value, "model_dump", None))

    if model_dump is not None:
        return model_dump()

    result: dict[str, Any] = {}
    for key in ("expanders", "pins"):
        if hasattr(value, key):
            result[key] = getattr(value, key)

    return result


def _parse_expander(value: Any) -> GpioExpanderSpec:
    data = _to_plain_dict(value)
    interrupt_data = data.get("interrupt") or data.get("interrupt_pin")
    interrupt = _parse_interrupt(interrupt_data) if interrupt_data is not None else None

    return GpioExpanderSpec(
        name=str(data["name"]),
        kind=data.get("type", data.get("kind", "pca9535")),
        bus=int(data.get("bus", 1)),
        address=int(data.get("address", 0x20)),
        interrupt=interrupt,
    )


def _parse_interrupt(value: Any) -> GpioInterruptSpec:
    data = _to_plain_dict(value)

    return GpioInterruptSpec(
        board=data.get("board", "rpi"),
        pin=int(data["pin"]),
        pull=data.get("pull", "up"),
        edge=data.get("edge", "falling"),
        debounce_ms=int(data.get("debounce_ms", 0)),
    )


def _parse_pin(name: str, value: Any) -> GpioPinSpec:
    data = _to_plain_dict(value)

    return GpioPinSpec(
        name=name,
        board=data["board"],
        pin=int(data["pin"]),
        direction=data["direction"],
        expander=data.get("expander"),
        pull=data.get("pull", "off"),
        inverted=bool(data.get("inverted", False)),
        debounce_ms=int(data.get("debounce_ms", 0)),
        initial=data.get("initial"),
        shutdown=data.get("shutdown"),
        required=bool(data.get("required", True)),
    )


def _to_plain_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)

    model_dump = cast(Callable[[], dict[str, Any]] | None, getattr(value, "model_dump", None))

    if model_dump is not None:
        return model_dump()

    return dict(vars(value))
