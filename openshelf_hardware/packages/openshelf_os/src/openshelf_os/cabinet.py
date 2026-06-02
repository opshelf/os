from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hardware.gpio.types import DigitalInput, DigitalOutput, GpioEvent, GpioSubscription
from openshelf_os.exceptions import ConfigError


@dataclass
class CabinetDoorLock:
    unlock_output: DigitalOutput
    closed_input: DigitalInput

    def lock(self) -> None:
        self.unlock_output.write(False)

    def unlock(self) -> None:
        self.unlock_output.write(True)

    def is_closed(self) -> bool:
        return self.closed_input.read()


class Cabinet:
    def __init__(self, *, gpio: Any = None) -> None:
        self._gpio = gpio
        self._door_locks: dict[str, CabinetDoorLock] = {}
        self._door_listener: Callable[[dict[str, bool]], None] | None = None
        self._door_state: dict[str, bool] | None = None
        self._door_subscriptions: list[GpioSubscription] = []
        self._door_monitor_task: asyncio.Task | None = None
        self._door_poll_interval_s = 0.05
        self._logger = logging.getLogger(__name__).getChild("Cabinet")

    async def configure(self) -> None:
        try:
            self._require_door_locks()
        except ConfigError as exc:
            self._logger.info("Cabinet door monitor disabled: %s", exc)
            return
        self._handle_door_state(self.get_door_state())

        try:
            self._door_subscriptions = [
                door_lock.closed_input.subscribe(self._did_change_door_input)
                for door_lock in self._door_locks.values()
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

    def _require_door_locks(self) -> dict[str, CabinetDoorLock]:
        if self._door_locks:
            return self._door_locks

        if self._gpio is None:
            raise ConfigError("Cabinet GPIO is not configured")

        lock_specs = {
            "top": ("cabinet_top_unlock", "cabinet_top_closed"),
            "bottom": ("cabinet_bot_unlock", "cabinet_bot_closed"),
        }

        missing = [
            pin_name
            for names in lock_specs.values()
            for pin_name in names
            if self._gpio.has_pin(pin_name) is False
        ]
        if missing:
            raise ConfigError("Cabinet GPIO pins are not configured: " + ", ".join(missing))

        self._door_locks = {
            name: CabinetDoorLock(
                unlock_output=self._gpio.output(unlock_name),
                closed_input=self._gpio.input(closed_name),
            )
            for name, (unlock_name, closed_name) in lock_specs.items()
        }
        return self._door_locks

    def get_door_state(self) -> dict[str, bool]:
        door_locks = self._require_door_locks()
        top_closed = door_locks["top"].is_closed()
        bottom_closed = door_locks["bottom"].is_closed()
        return {
            "top_closed": top_closed,
            "bottom_closed": bottom_closed,
            "closed": top_closed and bottom_closed,
            "top_open": top_closed is False,
            "bottom_open": bottom_closed is False,
            "open": top_closed is False or bottom_closed is False,
        }

    def get_status(self) -> dict[str, dict[str, bool]]:
        return {"doors": self.get_door_state()}

    async def lock(self) -> dict[str, bool]:
        door_locks = self._require_door_locks()
        for door_lock in door_locks.values():
            door_lock.lock()
        return self.get_door_state()

    async def unlock(self) -> dict[str, bool]:
        door_locks = self._require_door_locks()
        for door_lock in door_locks.values():
            door_lock.unlock()
        return self.get_door_state()

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
