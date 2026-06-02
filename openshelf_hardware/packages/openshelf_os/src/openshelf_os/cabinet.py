from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hardware.gpio.types import DigitalInput, DigitalOutput, GpioEvent, GpioSubscription
from openshelf_os.exceptions import ConfigError


@dataclass
class CabinetDoorLatch:
    unlock_output: DigitalOutput
    closed_input: DigitalInput

    def set_passive(self) -> None:
        self.unlock_output.write(False)

    def release(self) -> None:
        self.unlock_output.write(True)

    def is_closed(self) -> bool:
        return self.closed_input.read()

    def is_released(self) -> bool:
        return self.unlock_output.read()


class Cabinet:
    def __init__(self, *, gpio: Any = None) -> None:
        self._gpio = gpio
        self._door_latches: dict[str, CabinetDoorLatch] = {}
        self._door_listener: Callable[[dict[str, bool]], None] | None = None
        self._door_state: dict[str, bool] | None = None
        self._door_subscriptions: list[GpioSubscription] = []
        self._door_monitor_task: asyncio.Task | None = None
        self._door_poll_interval_s = 0.05
        self._logger = logging.getLogger(__name__).getChild("Cabinet")

    async def configure(self) -> None:
        try:
            self._require_door_latches()
        except ConfigError as exc:
            self._logger.info("Cabinet door monitor disabled: %s", exc)
            return
        self._handle_door_state(self.get_door_state())

        try:
            self._door_subscriptions = [
                door_latch.closed_input.subscribe(self._did_change_door_input)
                for door_latch in self._door_latches.values()
            ]
        except Exception:
            self._close_door_subscriptions()
            self._logger.warning(
                "Cabinet door subscription failed, falling back to polling",
                exc_info=True,
            )
            self._door_monitor_task = asyncio.create_task(
                self._monitor_doors(),
                name="cabinet-door-monitor",
            )

    async def stop(self) -> None:
        self._close_door_subscriptions()

        if self._door_monitor_task is None:
            return

        self._door_monitor_task.cancel()
        try:
            await self._door_monitor_task
        except asyncio.CancelledError:
            pass
        self._door_monitor_task = None

    def set_door_listener(self, listener: Callable[[dict[str, bool]], None]) -> None:
        self._door_listener = listener

        if self._door_state is not None:
            self._emit_door_state(self._door_state)

    def _require_door_latches(self) -> dict[str, CabinetDoorLatch]:
        if self._door_latches:
            return self._door_latches

        if self._gpio is None:
            raise ConfigError("Cabinet GPIO is not configured")

        latch_specs = {
            "top": ("cabinet_top_unlock", "cabinet_top_closed"),
            "bottom": ("cabinet_bot_unlock", "cabinet_bot_closed"),
        }

        missing = [
            pin_name
            for names in latch_specs.values()
            for pin_name in names
            if self._gpio.has_pin(pin_name) is False
        ]
        if missing:
            raise ConfigError("Cabinet GPIO pins are not configured: " + ", ".join(missing))

        self._door_latches = {
            name: CabinetDoorLatch(
                unlock_output=self._gpio.output(unlock_name),
                closed_input=self._gpio.input(closed_name),
            )
            for name, (unlock_name, closed_name) in latch_specs.items()
        }
        return self._door_latches

    def get_door_state(self) -> dict[str, bool]:
        door_latches = self._require_door_latches()
        top_closed = door_latches["top"].is_closed()
        bottom_closed = door_latches["bottom"].is_closed()
        return {
            "top_closed": top_closed,
            "bottom_closed": bottom_closed,
            "closed": top_closed and bottom_closed,
            "top_open": top_closed is False,
            "bottom_open": bottom_closed is False,
            "open": top_closed is False or bottom_closed is False,
        }

    def get_latch_state(self) -> dict[str, bool]:
        door_latches = self._require_door_latches()
        top_released = door_latches["top"].is_released()
        bottom_released = door_latches["bottom"].is_released()
        top_engaged = door_latches["top"].is_closed()
        bottom_engaged = door_latches["bottom"].is_closed()
        return {
            "top_released": top_released,
            "bottom_released": bottom_released,
            "released": top_released or bottom_released,
            "top_engaged": top_engaged,
            "bottom_engaged": bottom_engaged,
            "engaged": top_engaged and bottom_engaged,
        }

    def get_status(self) -> dict[str, dict[str, bool]]:
        return {"doors": self.get_door_state(), "latches": self.get_latch_state()}

    async def set_passive(self) -> dict[str, dict[str, bool]]:
        door_latches = self._require_door_latches()
        for door_latch in door_latches.values():
            door_latch.set_passive()
        return self.get_status()

    async def release(self) -> dict[str, dict[str, bool]]:
        door_latches = self._require_door_latches()
        for door_latch in door_latches.values():
            door_latch.release()
        return self.get_status()

    def _did_change_door_input(self, event: GpioEvent) -> None:
        del event
        self._handle_door_state(self.get_door_state())

    def _handle_door_state(self, state: dict[str, bool]) -> None:
        if state == self._door_state:
            return

        previous = self._door_state
        self._door_state = state
        self._logger.debug(
            "Cabinet door state change",
            extra={"state": state, "previous": previous},
        )
        self._emit_door_state(state)

    def _emit_door_state(self, state: dict[str, bool]) -> None:
        if self._door_listener is None:
            return

        try:
            self._door_listener(state)
        except Exception as exc:
            self._logger.warning("Cabinet door listener failed: %s", exc)

    async def _monitor_doors(self) -> None:
        try:
            while True:
                self._handle_door_state(self.get_door_state())
                await asyncio.sleep(self._door_poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._logger.warning("Cabinet door monitor crashed: %s", exc, exc_info=True)

    def _close_door_subscriptions(self) -> None:
        for subscription in self._door_subscriptions:
            try:
                subscription.close()
            except Exception:
                pass
        self._door_subscriptions = []
