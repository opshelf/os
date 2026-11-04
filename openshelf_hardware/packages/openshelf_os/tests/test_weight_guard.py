"""Weight-change guard tests: detection, acknowledgement and safety-guard state."""
# ruff: noqa: E402

import asyncio
import pathlib
import sys
import tempfile
import unittest
from typing import Any, NamedTuple, Optional

# Ensure workspace src paths are importable when running tests directly.
BASE = pathlib.Path(__file__).resolve()
sys.path.insert(0, str(BASE.parents[1] / "src"))
sys.path.insert(0, str(BASE.parents[3] / "packages" / "hardware" / "src"))

from openshelf_os.robot.weight_guard import WeightChangeGuard
from openshelf_os.robot.worker import RobotWorker

SOURCE = "weight_change"


class FakeReading(NamedTuple):
    """Duck-typed stand-in for hardware.scale.WeightReading."""

    grams: float
    decimals: int = 0
    stable: bool = True
    valid: bool = True
    overload: bool = False


class FakeLock:
    def __init__(self) -> None:
        self.held = False

    def locked(self) -> bool:
        return self.held


class FakeScale:
    def __init__(self, readings: Optional[list[Any]] = None, failure: Optional[Exception] = None) -> None:
        self.modbus_controller = type("Controller", (), {"lock": FakeLock()})()
        self.readings = list(readings or [])
        self.failure = failure
        self.reads = 0

    async def read_weight_fast(self) -> FakeReading:
        self.reads += 1

        if self.failure is not None:
            raise self.failure

        if not self.readings:
            raise AssertionError("FakeScale ran out of readings")

        item = self.readings.pop(0)

        if isinstance(item, Exception):
            raise item

        return item


class FakeAxis:
    def __init__(self) -> None:
        self.moving = False
        self.position = 412.5
        self.direction = 0

    def is_moving(self) -> bool:
        return self.moving

    def position_mm(self) -> Optional[float]:
        return self.position

    def commanded_direction(self) -> int:
        return self.direction if self.moving else 0


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class GuardHarness:
    """Guard plus its fakes, with the trip record written to a temp dir."""

    def __init__(
        self,
        *,
        readings: Optional[list[Any]] = None,
        failure: Optional[Exception] = None,
        delta_grams: float = 10.0,
        min_samples: int = 1,
        arm_delay_s: float = 0.0,
        ack_grace_s: float = 1.0,
    ) -> None:
        self.clock = FakeClock()
        self.scale = FakeScale(readings, failure)
        self.axis = FakeAxis()
        self.trips: list[dict[str, Any]] = []
        self.records = tempfile.TemporaryDirectory()
        self.flushed_path: Optional[str] = None
        self.guard = WeightChangeGuard(
            scale=self.scale,
            axis=self.axis,
            delta_grams=delta_grams,
            min_samples=min_samples,
            arm_delay_s=arm_delay_s,
            ack_grace_s=ack_grace_s,
            record_dir=pathlib.Path(self.records.name),
            context_provider=lambda: {"phase": "io_place.y_axis_transfer"},
            clock=self.clock,
        )
        self.guard.set_listener(self.trips.append)

    def sample(
        self,
        grams: float,
        *,
        moving: bool,
        direction: int = -1,
        travel_mm: Optional[float] = None,
    ) -> None:
        """One guard sample; a moving sample travels 1 mm in `direction` first by default."""
        self.axis.moving = moving
        self.axis.direction = direction

        if moving is True:
            self.axis.position += direction * 1.0 if travel_mm is None else travel_mm

        self.guard._sample(FakeReading(grams=grams), moving=moving)


    def cleanup(self) -> None:
        self.records.cleanup()


class WeightChangeDirectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GuardHarness()
        self.addCleanup(self.harness.cleanup)


    def test_crash_that_stops_the_descent_still_trips(self):
        self.harness.guard._min_samples = 3
        self.harness.sample(100.0, moving=False)
        self.harness.sample(100.0, moving=True, travel_mm=-20.0)
        for grams in (130.0, 160.0, 190.0):
            self.harness.sample(grams, moving=True, travel_mm=0.0)

        self.assertTrue(self.harness.guard.is_latched)


class FakeGuardRobot:
    """Robot double exposing the weight-change surface the worker uses."""

    def __init__(self, *, weight_state: Optional[dict[str, Any]] = None) -> None:
        self._weight_state = weight_state
        self._curtain_state = False
        self._door_state = False
        self.safety_guard_bypassed = False
        self.light_curtain_bypassed = False
        self._cabinet_doors = {
            "top_closed": True,
            "bottom_closed": True,
            "closed": True,
            "top_open": False,
            "bottom_open": False,
            "open": False,
        }
        self.pause_calls = 0
        self.resume_calls = 0
        self.acknowledge_calls = 0
        self.listeners: list[Any] = []

    def get_curtain_state(self) -> bool:
        return self._curtain_state

    async def refresh_door_state(self) -> bool:
        return self._door_state

    def get_cabinet_door_state(self) -> dict[str, bool]:
        return dict(self._cabinet_doors)

    def get_operation_context(self) -> Optional[dict[str, Any]]:
        return None

    def block_motion_for_safety(self) -> None:
        self.pause_calls += 1

    def release_motion_safety(self) -> None:
        self.resume_calls += 1

    def set_safety_guard_bypassed(self, enabled: bool) -> None:
        self.safety_guard_bypassed = enabled

    def set_light_curtain_bypassed(self, enabled: bool) -> None:
        self.light_curtain_bypassed = enabled

    def register_weight_change_listener(self, listener) -> None:
        self.listeners.append(listener)

    def get_weight_change_state(self) -> Optional[dict[str, Any]]:
        if self._weight_state is None:
            return None

        return dict(self._weight_state)

    def acknowledge_weight_change(self) -> None:
        self.acknowledge_calls += 1

        if self._weight_state is not None:
            self._weight_state = {**self._weight_state, "active": False}

    async def start_weight_guard(self) -> None:
        pass

    async def stop_weight_guard(self) -> None:
        pass


def tripped_state(*, delta_grams: float = 42.0) -> dict[str, Any]:
    return {
        "active": True,
        "axis": "y_axis",
        "clears_on_ack": True,
        "delta_grams": delta_grams,
        "weight_grams": 142.0,
        "baseline_grams": 100.0,
        "threshold_grams": 10.0,
        "axis_position_mm": 900.0,
        "detected_at": "2026-10-30T09:00:00+00:00",
        "record_path": None,
        "error": None,
    }


class LiveScale:
    """Scale double whose reading can be moved while the guard task runs."""

    def __init__(self, grams: float) -> None:
        self.grams = grams
        self.reads = 0
        self.modbus_controller = type("Controller", (), {"lock": asyncio.Lock()})()

    async def read_weight_fast(self) -> FakeReading:
        self.reads += 1
        await asyncio.sleep(0)

        return FakeReading(grams=self.grams)


class WeightChangeGuardLoopTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the sampling task itself, with the real event loop clock."""

    async def test_loop_trips_during_motion_and_watches_again_after_acknowledgement(self):
        records = tempfile.TemporaryDirectory()
        self.addCleanup(records.cleanup)
        scale = LiveScale(100.0)
        axis = FakeAxis()
        trips: list[dict] = []
        guard = WeightChangeGuard(
            scale=scale,
            axis=axis,
            delta_grams=10.0,
            min_samples=1,
            arm_delay_s=0.0,
            ack_grace_s=0.05,
            idle_interval_s=0.01,
            record_dir=pathlib.Path(records.name),
        )
        guard.set_listener(trips.append)

        await guard.start()
        self.addAsyncCleanup(guard.stop)

        # At rest first, as on a running machine, so the guard knows where the move starts.
        await asyncio.sleep(0.03)
        axis.moving = True
        axis.direction = -1
        axis.position -= 10.0
        await asyncio.sleep(0.05)
        self.assertGreater(scale.reads, 3)

        scale.grams = 130.0
        await asyncio.sleep(0.05)

        self.assertEqual(len(trips), 1)
        self.assertTrue(guard.is_latched)
        self.assertEqual(guard.source_state()["active"], True)
        self.assertEqual(len(list(pathlib.Path(records.name).glob("*.json"))), 1)

        # A latched guard records but does not keep re-tripping while it waits.
        await asyncio.sleep(0.05)
        self.assertEqual(len(trips), 1)

        guard.acknowledge()
        scale.grams = 131.0
        await asyncio.sleep(0.15)

        self.assertEqual(len(trips), 1)
        self.assertFalse(guard.is_latched)

        scale.grams = 146.0
        await asyncio.sleep(0.05)

        self.assertEqual(len(trips), 2)
        self.assertAlmostEqual(trips[1]["delta_grams"], 15.0, places=0)

        await guard.stop()
        self.assertIsNone(guard._task)


class WeightChangeGuardWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.robot = FakeGuardRobot(weight_state=tripped_state())
        self.worker = RobotWorker(self.robot)
        self.published: list[int] = []

        async def fake_publish_status() -> None:
            self.published.append(1)

        self.worker._publish_status = fake_publish_status  # type: ignore[method-assign]

    async def drain(self) -> None:
        tasks = list(self.worker._background_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    async def trip(self) -> None:
        self.worker._on_weight_change({"delta_grams": 42.0})
        self.assertEqual(self.robot.pause_calls, 1)
        await self.drain()

    async def test_trip_engages_the_interlock_before_the_first_await(self):
        await self.trip()

        safety = self.worker.get_safety_status()
        self.assertEqual(safety["active_sources"], [SOURCE])
        self.assertTrue(safety["awaiting_ack"])
        self.assertTrue(safety["motion_blocked"])
        self.assertTrue(safety["active"])
        self.assertEqual(len(self.published), 1)


    async def test_acknowledgement_clears_a_suppressed_weight_source(self):
        await self.trip()
        worker = self.worker
        worker._is_crashed = True
        await worker._evaluate_safety_guard(refresh=False)

        self.assertEqual(worker.get_safety_status()["active_sources"], [])

        result = await worker.acknowledge_safety_guard()

        self.assertTrue(result["accepted"])
        self.assertEqual(self.robot.acknowledge_calls, 1)

        worker._is_crashed = False
        await worker._evaluate_safety_guard(refresh=False)

        safety = worker.get_safety_status()
        self.assertEqual(safety["active_sources"], [])
        self.assertFalse(safety["sources"][SOURCE]["active"])


if __name__ == "__main__":
    unittest.main()
