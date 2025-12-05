from __future__ import annotations
import asyncio
import logging
from typing import Optional

try:
    import RPi.GPIO as GPIO  # type: ignore[import]
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
except (ModuleNotFoundError, RuntimeError):
    GPIO = None  # type: ignore[assignment]

logger = logging.getLogger(__name__).getChild("curtain")


class CurtainSensor:
    """
    Lightweight wrapper around the curtain GPIO input.

    Raises on construction if GPIO is unavailable.
    """

    def __init__(
        self,
        *,
        pin: int = 2,
    ) -> None:
        if GPIO is None:
            raise RuntimeError("RPi.GPIO not available")
        self.pin = pin
        GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
        self._last_state: Optional[bool] = None

    def read(self) -> bool:
        raw = GPIO.input(self.pin) == GPIO.HIGH  # type: ignore[arg-type]
        self._last_state = raw
        return raw

    def cleanup(self) -> None:
        GPIO.cleanup(self.pin)  # type: ignore[arg-type]



class CurtainMonitor:
    """Polls a CurtainSensor and invokes a sync callback on state change."""

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
        self._last: Optional[bool] = None

    async def start(self) -> None:
        if self._task:
            return
        self._last = self._sensor.read()
        self._on_change(self._last)
        logger.info(
            "Curtain monitor started",
            extra={
                "pin": getattr(self._sensor, "pin", None),
                "poll_interval_s": self._poll_interval,
                "initial_blocked": self._last,
            },
        )
        self._task = asyncio.create_task(self._run(), name="curtain-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info(
                "Curtain monitor stopped",
                extra={"pin": getattr(self._sensor, "pin", None)},
            )
            self._task = None

    async def _run(self) -> None:
        try:
            while True:
                state = self._sensor.read()
                if state != self._last:
                    self._last = state
                    self._on_change(state)
                await asyncio.sleep(self._poll_interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning(
                "Curtain monitor crashed",
                extra={"error": str(exc), "pin": getattr(self._sensor, "pin", None)},
                exc_info=True,
            )
