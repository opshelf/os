from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from hardware.gpio import DigitalInput, GpioEvent, GpioManager, GpioSubscription

logger = logging.getLogger(__name__).getChild("curtain")


class CurtainSafety:
    def __init__(self) -> None:
        self._clear = asyncio.Event()
        self._clear.set()
        self.blocked = False

    def block(self) -> None:
        self.blocked = True
        self._clear.clear()

    def unblock(self) -> None:
        self.blocked = False
        self._clear.set()

    async def wait_clear(self) -> None:
        await self._clear.wait()


class CurtainSensor:
    def __init__(
        self,
        *,
        pin: int = 2,
        inverted: bool = False,
        input_device: DigitalInput | None = None,
    ) -> None:
        self._owned_manager: GpioManager | None = None
        if input_device is None:
            self._owned_manager = GpioManager.for_legacy_rpi_input(
                "curtain",
                pin=pin,
                inverted=inverted,
                pull="down",
            )
            input_device = self._owned_manager.input("curtain")

        self._input = input_device
        self.pin = input_device.pin
        self._last_state: Optional[bool] = None

    def read(self) -> bool:
        result = self._input.read()
        self._last_state = result
        return result

    def subscribe(self, on_change: Callable[[bool], None]) -> GpioSubscription:
        def did_change(event: GpioEvent) -> None:
            self._last_state = event.value
            on_change(event.value)

        return self._input.subscribe(did_change)

    def cleanup(self) -> None:
        if self._owned_manager is None:
            return

        self._owned_manager.close()
        self._owned_manager = None


class CurtainMonitor:
    def __init__(
        self,
        sensor: CurtainSensor,
        *,
        poll_interval: float = 0.01,
        on_change,
    ) -> None:
        self._sensor = sensor
        self._poll_interval = poll_interval
        self._on_change = on_change
        self._task: Optional[asyncio.Task] = None
        self._subscription: GpioSubscription | None = None
        self._last: Optional[bool] = None

    async def start(self) -> None:
        if self._task or self._subscription:
            return

        self._last = self._sensor.read()
        self._on_change(self._last)

        try:
            self._subscription = self._sensor.subscribe(self._handle_change)
        except Exception:
            logger.warning(
                "Curtain interrupt subscription failed, falling back to polling",
                extra={"pin": getattr(self._sensor, "pin", None)},
                exc_info=True,
            )
            self._task = asyncio.create_task(self._run(), name="curtain-monitor")

        logger.info(
            "Curtain monitor started",
            extra={
                "pin": getattr(self._sensor, "pin", None),
                "poll_interval_s": self._poll_interval,
                "initial_blocked": self._last,
                "subscription_enabled": self._subscription is not None,
            },
        )

    async def stop(self) -> None:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None

        if self._task:
            self._task.cancel()

            try:
                await self._task
            except asyncio.CancelledError:
                pass

            self._task = None

        logger.info(
            "Curtain monitor stopped",
            extra={"pin": getattr(self._sensor, "pin", None)},
        )

    def _handle_change(self, state: bool) -> None:
        if state == self._last:
            return

        self._last = state
        self._on_change(state)

    async def _run(self) -> None:
        try:
            while True:
                state = self._sensor.read()
                self._handle_change(state)
                await asyncio.sleep(self._poll_interval)

        except asyncio.CancelledError:
            raise

        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning(
                "Curtain monitor crashed",
                extra={"error": str(exc), "pin": getattr(self._sensor, "pin", None)},
                exc_info=True,
            )
