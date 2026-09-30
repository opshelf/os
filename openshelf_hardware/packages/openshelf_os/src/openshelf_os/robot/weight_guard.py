"""Weight-change guard for y-axis travel.

The y axis is powerful enough to destroy the weighing cell when something is
wedged between the gantry and the scale, which in practice happens when a bottle
or a tray is dropped in the IO box. The cell itself is the earliest sensor for
that event, so while y is in motion this samples the scale as fast as the bus
allows and raises the same interruption the light curtain does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    NamedTuple,
    Optional,
)

if TYPE_CHECKING:
    from hardware.motor import Motor
    from hardware.scale import Scale, WeightReading

logger = logging.getLogger(__name__).getChild("weight_guard")

WEIGHT_CHANGE_SOURCE = "weight_change"
WEIGHT_FAULT_DUMP_DIR = Path("data/weight_faults")
WEIGHT_RECORD_SIZE = 512
# With the axis moving there is nothing to wait for: the Modbus round trip on the
# scale bus is the sample clock.
MOTION_TICK_S = 0.002
READ_ERROR_SUSPEND_THRESHOLD = 5
READ_ERROR_SUSPEND_S = 5.0
READ_ERROR_SUSPEND_MAX_S = 30.0
# Longer than this without a reading means the weight could have changed
# legitimately while nothing was watching, so the baseline is retaken rather than
# compared against a stale sample.
BASELINE_MAX_GAP_S = 1.0

WeightTripListener = Callable[[dict[str, Any]], None]


class WeightSample(NamedTuple):
    elapsed_s: float
    weight_grams: float
    delta_grams: Optional[float]
    axis_position_mm: Optional[float]
    stable: bool
    valid: bool


class WeightChangeGuard:
    """Watch the scale while the y axis moves and interrupt on a sudden delta.

    The baseline is the weight read while the axis was last idle, so a legitimate
    deposit or lift that happens during travel is measured against the resting
    weight of the machine rather than against a reading taken mid-move.
    """

    def __init__(
        self,
        *,
        scale: Scale,
        axis: Motor,
        axis_name: str = "y_axis",
        delta_grams: float = 10.0,
        min_samples: int = 2,
        arm_delay_s: float = 0.05,
        ack_grace_s: float = 1.0,
        sample_timeout_s: float = 0.25,
        idle_interval_s: float = 0.2,
        record_dir: Path = WEIGHT_FAULT_DUMP_DIR,
        record_size: int = WEIGHT_RECORD_SIZE,
        context_provider: Optional[Callable[[], dict[str, Any]]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scale = scale
        self._axis = axis
        self._axis_name = axis_name
        self._delta_grams = delta_grams
        self._min_samples = max(1, min_samples)
        self._arm_delay_s = arm_delay_s
        self._ack_grace_s = ack_grace_s
        self._sample_timeout_s = sample_timeout_s
        self._idle_interval_s = idle_interval_s
        self._record_dir = record_dir
        self._context_provider = context_provider
        self._clock = clock

        self._epoch = clock()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._listener: Optional[WeightTripListener] = None
        self._samples: deque[WeightSample] = deque(maxlen=record_size)
        self._baseline_grams: Optional[float] = None
        self._moving_since: Optional[float] = None
        self._grace_until: Optional[float] = None
        self._streak = 0
        self._latched = False
        self._trip_info: Optional[dict[str, Any]] = None
        self._record_path: Optional[str] = None
        self._last_error: Optional[str] = None
        self._read_error_streak = 0
        self._suspend_s = READ_ERROR_SUSPEND_S
        self._suspended_until: Optional[float] = None
        self._last_sample_at: Optional[float] = None

    # ------------------------------------------------------------------ #
    #  Lifecycle
    # ------------------------------------------------------------------ #

    def set_listener(self, listener: Optional[WeightTripListener]) -> None:
        self._listener = listener

    @property
    def is_latched(self) -> bool:
        return self._latched

    async def start(self) -> None:
        if self._task is not None:
            return

        self._running = True
        self._task = asyncio.create_task(self._run(), name="weight-change-guard")
        logger.info(
            "Weight change guard started",
            extra={
                "axis": self._axis_name,
                "threshold_grams": self._delta_grams,
                "min_samples": self._min_samples,
                "arm_delay_s": self._arm_delay_s,
                "ack_grace_s": self._ack_grace_s,
            },
        )

    async def stop(self) -> None:
        self._running = False

        if self._task is None:
            return

        self._task.cancel()

        try:
            await self._task
        except asyncio.CancelledError:
            pass

        self._task = None
        logger.info("Weight change guard stopped", extra={"axis": self._axis_name})

    # ------------------------------------------------------------------ #
    #  Sampling
    # ------------------------------------------------------------------ #

    async def _run(self) -> None:
        while self._running is True:
            moving = self._axis.is_moving()
            reading = await self._read_weight()

            if reading is not None:
                self._sample(reading, moving=moving)

            await asyncio.sleep(MOTION_TICK_S if moving else self._idle_interval_s)

    async def _read_weight(self) -> Optional[WeightReading]:
        now = self._clock()

        if self._suspended_until is not None and now < self._suspended_until:
            return None

        controller = getattr(self._scale, "modbus_controller", None)
        lock = getattr(controller, "lock", None)

        # A weighing, tare or calibration already owns the scale bus. Queueing
        # behind it would stall sampling for seconds, so skip the tick instead.
        if lock is not None and lock.locked() is True:
            return None

        try:
            reading = await asyncio.wait_for(
                self._scale.read_weight_fast(),
                timeout=self._sample_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - hardware dependent
            self._note_read_error(exc)

            return None

        self._read_error_streak = 0
        self._suspend_s = READ_ERROR_SUSPEND_S
        self._suspended_until = None
        self._last_error = None

        return reading

    def _note_read_error(self, exc: Exception) -> None:
        self._last_error = str(exc)
        self._read_error_streak += 1

        if self._read_error_streak < READ_ERROR_SUSPEND_THRESHOLD:
            return

        # A scale that is not answering must not turn into 30 failed reads a
        # second for every move; back off and let the slow poll report it.
        suspend_s = self._suspend_s
        self._suspended_until = self._clock() + suspend_s
        self._suspend_s = min(suspend_s * 2, READ_ERROR_SUSPEND_MAX_S)
        self._read_error_streak = 0
        logger.warning(
            "Weight change guard sampling suspended after repeated read failures",
            extra={
                "axis": self._axis_name,
                "suspend_s": suspend_s,
                "error": self._last_error,
            },
        )

    def _sample(self, reading: WeightReading, *, moving: bool) -> None:
        now = self._clock()
        gap_s = None if self._last_sample_at is None else now - self._last_sample_at
        self._last_sample_at = now
        delta = (
            None
            if self._baseline_grams is None
            else reading.grams - self._baseline_grams
        )
        self._samples.append(
            WeightSample(
                elapsed_s=now - self._epoch,
                weight_grams=reading.grams,
                delta_grams=delta,
                axis_position_mm=self._axis.position_mm(),
                stable=reading.stable,
                valid=reading.valid,
            )
        )

        if self._latched is True:
            # Keep recording, but the trip record is what the operator reads, so
            # the baseline stays frozen at the value it had when motion stopped.
            return

        if moving is False:
            self._baseline_grams = reading.grams
            self._moving_since = None
            self._streak = 0

            return

        if self._moving_since is None:
            self._moving_since = now

        if gap_s is not None and gap_s > BASELINE_MAX_GAP_S:
            self._baseline_grams = reading.grams
            self._streak = 0
            logger.debug(
                "Weight change guard rebased after a sampling gap",
                extra={"axis": self._axis_name, "gap_s": round(gap_s, 3)},
            )

            return

        if self._grace_until is not None and now < self._grace_until:
            self._baseline_grams = reading.grams
            self._streak = 0

            return

        if self._baseline_grams is None:
            self._baseline_grams = reading.grams

            return

        if now - self._moving_since < self._arm_delay_s:
            return

        delta = reading.grams - self._baseline_grams

        if abs(delta) < self._delta_grams:
            self._streak = 0

            return

        self._streak += 1

        if self._streak < self._min_samples:
            return

        self._trip(delta_grams=delta, weight_grams=reading.grams)

    def _trip(self, *, delta_grams: float, weight_grams: float) -> None:
        self._latched = True
        baseline_grams = self._baseline_grams
        info: dict[str, Any] = {
            "source": WEIGHT_CHANGE_SOURCE,
            "axis": self._axis_name,
            "delta_grams": round(delta_grams, 3),
            "weight_grams": round(weight_grams, 3),
            "baseline_grams": (
                None if baseline_grams is None else round(baseline_grams, 3)
            ),
            "threshold_grams": self._delta_grams,
            "sample_streak": self._streak,
            "axis_position_mm": self._axis.position_mm(),
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "record_path": None,
            "error": self._last_error,
        }
        self._trip_info = info

        # The listener stops the axes, so it runs before anything that can block.
        if self._listener is not None:
            self._listener(dict(info))

        self._record_path = self._dump(info)
        info["record_path"] = self._record_path

        logger.error(
            "Weight change interruption",
            extra={
                "axis": self._axis_name,
                "delta_grams": info["delta_grams"],
                "threshold_grams": self._delta_grams,
                "weight_grams": info["weight_grams"],
                "baseline_grams": info["baseline_grams"],
                "axis_position_mm": info["axis_position_mm"],
                "sample_streak": self._streak,
                "record_path": self._record_path,
            },
        )

    def acknowledge(self) -> None:
        """Accept the changed weight and keep watching from the new reading."""
        if self._latched is False and self._trip_info is None:
            return

        self._latched = False
        self._trip_info = None
        self._record_path = None
        self._streak = 0
        self._grace_until = self._clock() + self._ack_grace_s
        logger.info(
            "Weight change acknowledged",
            extra={
                "axis": self._axis_name,
                "grace_s": self._ack_grace_s,
            },
        )

    def source_state(self) -> dict[str, Any]:
        """Safety-guard view of the guard, in the shape the sources dict uses."""
        trip = self._trip_info or {}

        return {
            "active": self._latched,
            "axis": self._axis_name,
            # Acknowledging is what clears this source: the weight stays changed
            # after a crash, so requiring it to return would latch forever.
            "clears_on_ack": True,
            "delta_grams": trip.get("delta_grams"),
            "weight_grams": trip.get("weight_grams"),
            "baseline_grams": trip.get("baseline_grams"),
            "threshold_grams": self._delta_grams,
            "axis_position_mm": trip.get("axis_position_mm"),
            "detected_at": trip.get("detected_at"),
            "record_path": self._record_path,
            "error": self._last_error,
        }

    # ------------------------------------------------------------------ #
    #  Forensics
    # ------------------------------------------------------------------ #

    def _dump(self, trip: dict[str, Any]) -> Optional[str]:
        try:
            self._record_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now(timezone.utc)
            filename = (
                f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}-{self._axis_name}-weight-change.json"
            )
            path = self._record_dir / filename
            payload = {
                "created_at": now.isoformat(),
                "source": WEIGHT_CHANGE_SOURCE,
                "trip": trip,
                "threshold_grams": self._delta_grams,
                "min_samples": self._min_samples,
                "arm_delay_s": self._arm_delay_s,
                "ack_grace_s": self._ack_grace_s,
                "operation_context": (
                    self._context_provider() if self._context_provider is not None else {}
                ),
                "sample_count": len(self._samples),
                "samples": [sample._asdict() for sample in self._samples],
            }
            path.write_text(json.dumps(payload, indent=2))

            return str(path)
        except Exception as exc:  # pragma: no cover - filesystem dependent
            logger.warning(
                "Failed to write weight change record",
                extra={"error": str(exc), "axis": self._axis_name},
            )

            return None
