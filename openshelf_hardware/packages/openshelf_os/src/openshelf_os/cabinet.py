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
    def __init__(self, *, gpio: Any = None, door_debounce_s: float = 0.05) -> None:
        self._gpio = gpio
        self._door_latches: dict[str, CabinetDoorLatch] = {}
        self._door_listener: Callable[[dict[str, bool]], None] | None = None
        self._door_state: dict[str, bool] | None = None
        self._door_candidate_state: dict[str, bool] | None = None
        self._door_subscriptions: list[GpioSubscription] = []
        self._door_monitor_task: asyncio.Task | None = None
        self._door_debounce_task: asyncio.Task | None = None
        self._door_poll_interval_s = 0.05
        self._door_debounce_s = door_debounce_s
        self._release_pulse_s = 0.25
        self._logger = logging.getLogger(__name__).getChild("Cabinet")

    async def configure(self) -> None:
        try:
            self._require_door_latches()
        except ConfigError:
            self._logger.debug("Cabinet door monitor disabled: GPIO is not configured")
            return
        self._handle_door_state(self.get_door_state())

        self._door_subscriptions = []
        try:
            for door_latch in self._door_latches.values():
                self._door_subscriptions.append(
                    door_latch.closed_input.subscribe(self._did_change_door_input)
                )
        except Exception:
            self._close_door_subscriptions()
            self._logger.warning(
                "Cabinet door subscription failed; continuing with polling",
                exc_info=True,
            )

        self._door_monitor_task = asyncio.create_task(
            self._monitor_doors(),
            name="cabinet-door-monitor",
        )
        self._logger.info(
            "Cabinet door monitor started",
            extra={
                "subscription_count": len(self._door_subscriptions),
                "poll_interval_s": self._door_poll_interval_s,
                "polling_enabled": True,
            },
        )

    async def stop(self) -> None:
        self._close_door_subscriptions()
        await self._cancel_door_debounce()

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

    def is_configured(self) -> bool:
        try:
            self._require_door_latches()
        except ConfigError:
            return False

        return True

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
        try:
            await asyncio.sleep(self._release_pulse_s)
        finally:
            for door_latch in door_latches.values():
                door_latch.set_passive()
        return self.get_status()

    def _did_change_door_input(self, event: GpioEvent) -> None:
        del event
        self._handle_door_state(self.get_door_state(), debounce=True)

    def _handle_door_state(self, state: dict[str, bool], *, debounce: bool = False) -> None:
        if state == self._door_state:
            self._door_candidate_state = None
            self._cancel_door_debounce_soon()
            return

        if debounce is True and self._door_debounce_s > 0:
            self._schedule_door_debounce(state)
            return

        self._apply_door_state(state)

    def _apply_door_state(self, state: dict[str, bool]) -> None:
        if state == self._door_state:
            return

        previous = self._door_state
        self._door_state = state
        self._door_candidate_state = None
        self._logger.debug(
            "Cabinet door state change",
            extra={"state": state, "previous": previous},
        )
        self._emit_door_state(state)

    def _schedule_door_debounce(self, state: dict[str, bool]) -> None:
        if state == self._door_candidate_state:
            return

        self._door_candidate_state = state
        self._cancel_door_debounce_soon()
        self._door_debounce_task = asyncio.create_task(
            self._apply_door_state_after_debounce(state),
            name="cabinet-door-debounce",
        )

    async def _apply_door_state_after_debounce(self, state: dict[str, bool]) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self._door_debounce_s)
            if self.get_door_state() != state:
                return
            self._apply_door_state(state)
        except asyncio.CancelledError:
            raise
        finally:
            if self._door_debounce_task is task:
                self._door_debounce_task = None
                self._door_candidate_state = None

    def _cancel_door_debounce_soon(self) -> None:
        if self._door_debounce_task is None:
            return

        self._door_debounce_task.cancel()
        self._door_debounce_task = None

    async def _cancel_door_debounce(self) -> None:
        task = self._door_debounce_task
        self._door_debounce_task = None
        self._door_candidate_state = None

        if task is None:
            return

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _emit_door_state(self, state: dict[str, bool]) -> None:
        if self._door_listener is None:
            return

        try:
            self._door_listener(state)
        except Exception as exc:
            self._logger.warning("Cabinet door listener failed: %s", exc)

    async def _monitor_doors(self) -> None:
        poll_failed = False

        while True:
            await asyncio.sleep(self._door_poll_interval_s)

            try:
                self._handle_door_state(self.get_door_state(), debounce=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - hardware dependent
                if poll_failed is False:
                    self._logger.warning(
                        "Cabinet door poll failed; will retry: %s",
                        exc,
                        exc_info=True,
                    )
                poll_failed = True
            else:
                if poll_failed is True:
                    self._logger.info("Cabinet door polling recovered")
                poll_failed = False

    def _close_door_subscriptions(self) -> None:
        for subscription in self._door_subscriptions:
            try:
                subscription.close()
            except Exception:
                pass
        self._door_subscriptions = []
