from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from hardware.gpio import DigitalInput, GpioEvent, GpioSubscription

logger = logging.getLogger(__name__).getChild("curtain")


class MotionSafetyInterlock:
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
    def __init__(self, *, input_device: DigitalInput) -> None:
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
            self._subscription = self._sensor.subscribe(self._handle_interrupt_change)
        except Exception:
            logger.warning(
                "Curtain interrupt subscription failed; continuing with polling",
                extra={"pin": getattr(self._sensor, "pin", None)},
                exc_info=True,
            )

        # Keep polling even when interrupt subscription succeeds. GPIO expander
        # interrupts are an optimization for low-latency updates, not a reliable
        # source of truth: a missed/shared INT edge must not leave the safety
        # state frozen at its startup value.
        self._task = asyncio.create_task(self._run(), name="curtain-monitor")

        logger.info(
            "Curtain monitor started",
            extra={
                "pin": getattr(self._sensor, "pin", None),
                "poll_interval_s": self._poll_interval,
                "initial_blocked": self._last,
                "subscription_enabled": self._subscription is not None,
                "polling_enabled": self._task is not None,
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

    def _handle_interrupt_change(self, state: bool) -> None:
        self._handle_change(state, source="interrupt")

    def _handle_change(self, state: bool, *, source: str) -> None:
        if state == self._last:
            return

        previous = self._last
        self._last = state
        logger.info(
            "Curtain state changed",
            extra={
                "blocked": state,
                "previous": previous,
                "source": source,
                "pin": getattr(self._sensor, "pin", None),
            },
        )
        self._on_change(state)

    async def _run(self) -> None:
        poll_failed = False

        while True:
            await asyncio.sleep(self._poll_interval)

            try:
                state = self._sensor.read()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - hardware dependent
                if poll_failed is False:
                    logger.warning(
                        "Curtain poll failed; will retry",
                        extra={"error": str(exc), "pin": getattr(self._sensor, "pin", None)},
                        exc_info=True,
                    )
                poll_failed = True
            else:
                if poll_failed is True:
                    logger.info(
                        "Curtain polling recovered",
                        extra={"pin": getattr(self._sensor, "pin", None)},
                    )
                poll_failed = False
                self._handle_change(state, source="poll")
