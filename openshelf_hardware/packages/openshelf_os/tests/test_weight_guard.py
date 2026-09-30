"""Weight-change guard tests: detection, acknowledgement and safety-guard state."""
# ruff: noqa: E402

import asyncio
import json
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

    def is_moving(self) -> bool:
        return self.moving

    def position_mm(self) -> Optional[float]:
        return self.position


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta_s: float) -> None:
        self.now += delta_s


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

    def sample(self, grams: float, *, moving: bool) -> None:
        self.axis.moving = moving
        self.guard._sample(FakeReading(grams=grams), moving=moving)

    def record_payload(self) -> dict[str, Any]:
        paths = sorted(pathlib.Path(self.records.name).glob("*.json"))

        assert len(paths) == 1, [str(path) for path in paths]

        return json.loads(paths[0].read_text())

    def cleanup(self) -> None:
        self.records.cleanup()


class WeightChangeDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GuardHarness()
        self.addCleanup(self.harness.cleanup)

    def test_change_below_threshold_does_not_trip(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(106.0, moving=True)

        self.assertFalse(self.harness.guard.is_latched)
        self.assertEqual(self.harness.trips, [])

    def test_increase_over_threshold_trips(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(111.0, moving=True)

        self.assertTrue(self.harness.guard.is_latched)
        self.assertEqual(len(self.harness.trips), 1)
        trip = self.harness.trips[0]
        self.assertEqual(trip["source"], SOURCE)
        self.assertEqual(trip["axis"], "y_axis")
        self.assertAlmostEqual(trip["delta_grams"], 11.0)
        self.assertAlmostEqual(trip["baseline_grams"], 100.0)
        self.assertEqual(trip["axis_position_mm"], 412.5)

    def test_decrease_over_threshold_trips(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(89.0, moving=True)

        self.assertTrue(self.harness.guard.is_latched)
        self.assertAlmostEqual(self.harness.trips[0]["delta_grams"], -11.0)

    def test_trip_waits_for_consecutive_over_threshold_samples(self):
        self.harness.guard._min_samples = 3
        self.harness.sample(100.0, moving=False)
        self.harness.sample(140.0, moving=True)
        self.harness.sample(141.0, moving=True)

        self.assertFalse(self.harness.guard.is_latched)

        self.harness.sample(142.0, moving=True)

        self.assertTrue(self.harness.guard.is_latched)
        self.assertEqual(self.harness.trips[0]["sample_streak"], 3)

    def test_return_below_threshold_resets_the_streak(self):
        self.harness.guard._min_samples = 3
        self.harness.sample(100.0, moving=False)
        self.harness.sample(140.0, moving=True)
        self.harness.sample(101.0, moving=True)
        self.harness.sample(140.0, moving=True)
        self.harness.sample(140.0, moving=True)

        self.assertFalse(self.harness.guard.is_latched)

    def test_idle_weight_change_only_rebases_the_baseline(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(500.0, moving=False)
        self.harness.sample(502.0, moving=True)

        self.assertFalse(self.harness.guard.is_latched)

    def test_a_long_gap_in_sampling_rebases_instead_of_tripping(self):
        self.harness.sample(100.0, moving=True)
        self.harness.clock.advance(3.0)
        self.harness.sample(400.0, moving=True)

        self.assertFalse(self.harness.guard.is_latched)

        self.harness.sample(411.0, moving=True)

        self.assertTrue(self.harness.guard.is_latched)
        self.assertAlmostEqual(self.harness.trips[0]["delta_grams"], 11.0)

    def test_a_short_gap_between_samples_keeps_the_baseline(self):
        self.harness.sample(100.0, moving=True)
        self.harness.clock.advance(0.3)
        self.harness.sample(111.0, moving=True)

        self.assertTrue(self.harness.guard.is_latched)

    def test_move_start_jolt_inside_arm_delay_is_ignored(self):
        harness = GuardHarness(arm_delay_s=0.2)
        self.addCleanup(harness.cleanup)
        harness.sample(100.0, moving=False)
        harness.sample(140.0, moving=True)

        self.assertFalse(harness.guard.is_latched)

        harness.clock.advance(0.25)
        harness.sample(140.0, moving=True)

        self.assertTrue(harness.guard.is_latched)

    def test_trip_writes_a_record_with_samples_and_context(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(101.0, moving=True)
        self.harness.sample(135.0, moving=True)

        payload = self.harness.record_payload()
        self.assertEqual(payload["source"], SOURCE)
        self.assertEqual(payload["operation_context"], {"phase": "io_place.y_axis_transfer"})
        self.assertEqual(payload["threshold_grams"], 10.0)
        self.assertAlmostEqual(payload["trip"]["delta_grams"], 35.0)
        self.assertEqual(payload["sample_count"], 3)
        self.assertEqual(
            sorted(payload["samples"][1]),
            ["axis_position_mm", "delta_grams", "elapsed_s", "stable", "valid", "weight_grams"],
        )
        self.assertEqual(self.harness.guard.source_state()["record_path"].endswith(".json"), True)

    def test_source_state_reports_clears_on_ack(self):
        state = self.harness.guard.source_state()
        self.assertEqual(state["active"], False)
        self.assertEqual(state["clears_on_ack"], True)
        self.assertEqual(state["axis"], "y_axis")
        self.assertEqual(state["threshold_grams"], 10.0)

    def test_latched_state_keeps_recording_without_re_tripping(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(140.0, moving=True)
        self.harness.sample(160.0, moving=True)

        self.assertEqual(len(self.harness.trips), 1)
        self.assertEqual(self.harness.guard.source_state()["delta_grams"], 40.0)


class WeightChangeAcknowledgementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = GuardHarness(ack_grace_s=1.0)
        self.addCleanup(self.harness.cleanup)

    def test_acknowledge_clears_the_source_without_the_weight_returning(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(130.0, moving=True)
        self.assertTrue(self.harness.guard.is_latched)

        self.harness.guard.acknowledge()

        self.assertFalse(self.harness.guard.is_latched)
        self.assertEqual(self.harness.guard.source_state()["active"], False)

    def test_acknowledged_weight_that_stays_changed_does_not_re_trip(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(130.0, moving=True)
        self.harness.guard.acknowledge()

        # Step past the grace window in sample-sized increments so the readings
        # are compared, not merely rebased.
        for step_s, grams in ((0.2, 130.0), (0.2, 130.0), (0.2, 130.0), (0.4, 130.0), (0.2, 130.4)):
            self.harness.clock.advance(step_s)
            self.harness.sample(grams, moving=True)

        self.assertFalse(self.harness.guard.is_latched)
        self.assertEqual(len(self.harness.trips), 1)

    def test_further_change_after_acknowledgement_trips_again(self):
        self.harness.sample(100.0, moving=False)
        self.harness.sample(130.0, moving=True)
        self.harness.guard.acknowledge()

        for step_s, grams in ((0.2, 130.0), (0.2, 130.0), (0.2, 130.0), (0.4, 130.0), (0.2, 141.0)):
            self.harness.clock.advance(step_s)
            self.harness.sample(grams, moving=True)

        self.assertTrue(self.harness.guard.is_latched)
        self.assertEqual(len(self.harness.trips), 2)
        self.assertAlmostEqual(self.harness.trips[1]["delta_grams"], 11.0)

    def test_acknowledge_is_idempotent(self):
        self.harness.guard.acknowledge()

        self.assertFalse(self.harness.guard.is_latched)
        self.assertIsNone(self.harness.guard._grace_until)


class WeightChangeSamplingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_read_failures_suspend_sampling(self):
        harness = GuardHarness(failure=RuntimeError("No response received after 2 retries"))
        self.addCleanup(harness.cleanup)

        for _ in range(5):
            self.assertIsNone(await harness.guard._read_weight())

        self.assertEqual(harness.scale.reads, 5)
        self.assertIsNotNone(harness.guard._suspended_until)

        self.assertIsNone(await harness.guard._read_weight())
        self.assertEqual(harness.scale.reads, 5)
        self.assertEqual(harness.guard.source_state()["error"], "No response received after 2 retries")

        # A scale that stays dead is probed ever more rarely.
        self.assertEqual(harness.guard._suspend_s, 10.0)
        harness.clock.advance(6.0)

        for _ in range(5):
            self.assertIsNone(await harness.guard._read_weight())

        self.assertEqual(harness.guard._suspend_s, 20.0)

    async def test_a_reading_is_skipped_while_another_operation_holds_the_scale(self):
        harness = GuardHarness(readings=[FakeReading(grams=100.0)])
        self.addCleanup(harness.cleanup)

        harness.scale.modbus_controller.lock.held = True

        self.assertIsNone(await harness.guard._read_weight())
        self.assertEqual(harness.scale.reads, 0)

        harness.scale.modbus_controller.lock.held = False

        reading = await harness.guard._read_weight()

        self.assertIsNotNone(reading)


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

        axis.moving = True
        await asyncio.sleep(0.05)
        self.assertGreater(scale.reads, 3)

        scale.grams = 130.0
        await asyncio.sleep(0.05)

        self.assertEqual(len(trips), 1)
        self.assertTrue(guard.is_latched)
        self.assertEqual(guard.source_state()["active"], True)

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


class LegacyRobot(FakeGuardRobot):
    """Robot double from before the weight guard existed, without the new methods."""

    get_weight_change_state = None  # type: ignore[assignment]


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

    async def test_source_reports_the_measured_change(self):
        await self.trip()

        source = self.worker.get_safety_status()["sources"][SOURCE]
        self.assertEqual(source["delta_grams"], 42.0)
        self.assertEqual(source["threshold_grams"], 10.0)
        self.assertEqual(source["baseline_grams"], 100.0)
        self.assertEqual(source["clears_on_ack"], True)
        self.assertEqual(source["bypassed"], False)

    async def test_acknowledgement_releases_motion_even_with_weight_changed(self):
        await self.trip()

        # The worker only ever sees the guard's state, so mimic the guard having
        # re-based on the new reading.
        self.assertEqual(self.worker.get_safety_status()["can_acknowledge"], True)
        result = await self.worker.acknowledge_safety_guard()

        self.assertTrue(result["accepted"])
        self.assertEqual(self.robot.acknowledge_calls, 1)
        self.assertEqual(self.robot.resume_calls, 1)
        safety = self.worker.get_safety_status()
        self.assertEqual(safety["active_sources"], [])
        self.assertFalse(safety["awaiting_ack"])
        self.assertFalse(safety["motion_blocked"])

    async def test_light_curtain_still_has_to_clear_physically(self):
        self.robot._curtain_state = True
        self.robot._door_state = True
        # The trip path evaluates without refreshing inputs, so seed the cached
        # sensor reads the way the slow poll would have.
        self.worker._curtain_blocked = True
        self.worker._door_open = True

        await self.trip()

        safety = self.worker.get_safety_status()
        self.assertEqual(safety["active_sources"], ["light_curtain", SOURCE])
        self.assertFalse(safety["can_acknowledge"])

        result = await self.worker.acknowledge_safety_guard()

        self.assertFalse(result["accepted"])
        self.assertEqual(self.robot.acknowledge_calls, 0)
        self.assertEqual(self.robot.resume_calls, 0)

    async def test_curtain_clear_then_acknowledge_resumes(self):
        self.robot._curtain_state = True
        self.robot._door_state = True
        self.worker._curtain_blocked = True
        self.worker._door_open = True
        await self.trip()

        self.robot._curtain_state = False
        self.robot._door_state = False
        result = await self.worker.acknowledge_safety_guard()

        self.assertTrue(result["accepted"])
        self.assertEqual(self.robot.acknowledge_calls, 1)

    async def test_component_bypass_keeps_motion_running(self):
        await self.worker.set_safety_component_bypassed(SOURCE, True)

        self.worker._on_weight_change({"delta_grams": 42.0})
        await self.drain()

        safety = self.worker.get_safety_status()
        self.assertEqual(self.robot.pause_calls, 0)
        self.assertEqual(safety["active_sources"], [])
        self.assertTrue(safety["sources"][SOURCE]["bypassed"])

        await self.worker.set_safety_component_bypassed(SOURCE, False)

        safety = self.worker.get_safety_status()
        self.assertEqual(safety["active_sources"], [SOURCE])

    async def test_global_bypass_keeps_motion_running(self):
        await self.worker.set_safety_guard_bypassed(True)

        self.worker._on_weight_change({"delta_grams": 42.0})
        await self.drain()

        self.assertEqual(self.robot.pause_calls, 0)
        self.assertFalse(self.worker.get_safety_status()["active_sources"])

    async def test_source_is_absent_when_the_robot_has_no_guard(self):
        worker = RobotWorker(LegacyRobot())

        await worker._evaluate_safety_guard()

        self.assertNotIn(SOURCE, worker.get_safety_status()["sources"])

    async def test_crash_hides_the_weight_source_behind_the_crash_state(self):
        await self.trip()
        worker = self.worker
        worker._is_crashed = True

        await worker._evaluate_safety_guard(refresh=False)

        safety = worker.get_safety_status()
        self.assertEqual(safety["active_sources"], [])
        self.assertTrue(safety["sources"][SOURCE]["active"] is False)


if __name__ == "__main__":
    unittest.main()
