import json

import pytest

from openshelf_os_server.core.init_progress import (
    OVERALL_DEGRADED,
    OVERALL_INITIALIZING,
    OVERALL_READY,
    RobotInitTracker,
)


def test_register_and_report_transitions():
    tracker = RobotInitTracker()
    tracker.register_components(["motor_bus", "x_axis", "z_axis"])

    snapshot = tracker.snapshot()
    assert snapshot["phase"] == OVERALL_INITIALIZING
    assert [c["component"] for c in snapshot["components"]] == ["motor_bus", "x_axis", "z_axis"]
    assert all(c["phase"] == "pending" for c in snapshot["components"])

    tracker.report("motor_bus", "ready")
    tracker.report("x_axis", "homing")
    tracker.report("z_axis", "failed", reason="timeout")

    components = {c["component"]: c for c in tracker.snapshot()["components"]}
    assert components["motor_bus"]["phase"] == "ready"
    assert components["x_axis"]["phase"] == "homing"
    assert components["z_axis"]["phase"] == "failed"
    assert components["z_axis"]["reason"] == "timeout"


def test_failed_components_lists_only_failures():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis", "y_axis"])
    tracker.report("x_axis", "ready")
    tracker.report("y_axis", "failed", reason="fault")

    assert tracker.failed_components() == ["y_axis"]


def test_report_for_unregistered_component_is_added():
    tracker = RobotInitTracker()
    tracker.report("laser", "ready")

    names = [c["component"] for c in tracker.snapshot()["components"]]
    assert "laser" in names


def test_degraded_tracker_recovers_when_last_failure_is_ready():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis", "z_axis"])
    tracker.report("x_axis", "failed", reason="fault")
    tracker.report("z_axis", "ready")
    tracker.set_overall(OVERALL_DEGRADED)

    tracker.report("x_axis", "ready")

    assert tracker.failed_components() == []
    assert tracker.snapshot()["phase"] == OVERALL_READY


def test_degraded_tracker_stays_degraded_until_all_components_are_ready():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis", "z_axis"])
    tracker.report("x_axis", "failed", reason="fault")
    tracker.report("z_axis", "ready")
    tracker.set_overall(OVERALL_DEGRADED)

    tracker.report("x_axis", "homing")

    assert tracker.failed_components() == []
    assert tracker.snapshot()["phase"] == OVERALL_DEGRADED


def test_ready_tracker_becomes_degraded_when_component_fails():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis"])
    tracker.report("x_axis", "ready")
    tracker.set_overall(OVERALL_READY)

    tracker.report("x_axis", "failed", reason="fault")

    assert tracker.failed_components() == ["x_axis"]
    assert tracker.snapshot()["phase"] == OVERALL_DEGRADED


def test_non_degrading_component_failure_keeps_robot_ready():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis"])
    tracker.report("x_axis", "ready")
    tracker.set_overall(OVERALL_READY)

    tracker.register_components(["tool_camera"], degrade=False)
    tracker.report("tool_camera", "failed", reason="no frames within 15s")

    assert tracker.failed_components() == ["tool_camera"]
    assert tracker.failed_components(degrading_only=True) == []
    assert tracker.snapshot()["phase"] == OVERALL_READY


def test_degraded_tracker_recovers_when_only_non_degrading_component_failed():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis"])
    tracker.report("x_axis", "failed", reason="fault")
    tracker.set_overall(OVERALL_DEGRADED)

    tracker.register_components(["tool_camera"], degrade=False)
    tracker.report("tool_camera", "failed", reason="no frames within 15s")
    tracker.report("x_axis", "ready")

    assert tracker.failed_components() == ["tool_camera"]
    assert tracker.failed_components(degrading_only=True) == []
    assert tracker.snapshot()["phase"] == OVERALL_READY


def test_degrading_component_failure_still_degrades_with_non_degrading_components():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis"])
    tracker.register_components(["tool_camera"], degrade=False)
    tracker.report("x_axis", "ready")
    tracker.set_overall(OVERALL_READY)

    tracker.report("x_axis", "failed", reason="fault")

    assert tracker.failed_components() == ["x_axis"]
    assert tracker.failed_components(degrading_only=True) == ["x_axis"]
    assert tracker.snapshot()["phase"] == OVERALL_DEGRADED


def test_degraded_tracker_stays_degraded_until_all_failures_recover():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis", "z_axis"])
    tracker.report("x_axis", "failed", reason="fault")
    tracker.report("z_axis", "failed", reason="fault")
    tracker.set_overall(OVERALL_DEGRADED)

    tracker.report("x_axis", "ready")

    assert tracker.failed_components() == ["z_axis"]
    assert tracker.snapshot()["phase"] == OVERALL_DEGRADED


def test_snapshot_json_is_valid():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis"])
    tracker.set_overall(OVERALL_DEGRADED)

    parsed = json.loads(tracker.snapshot_json())
    assert parsed["phase"] == OVERALL_DEGRADED
    assert parsed["components"][0]["component"] == "x_axis"


@pytest.mark.asyncio
async def test_subscribe_receives_broadcasts():
    tracker = RobotInitTracker()
    queue = await tracker.subscribe()

    tracker.report("x_axis", "ready")

    payload = await queue.get()
    parsed = json.loads(payload)
    assert parsed["components"][0]["component"] == "x_axis"
    assert parsed["components"][0]["phase"] == "ready"

    await tracker.unsubscribe(queue)
