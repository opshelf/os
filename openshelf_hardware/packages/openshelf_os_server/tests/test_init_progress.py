from openshelf_os_server.core.init_progress import (
    OVERALL_DEGRADED,
    OVERALL_READY,
    RobotInitTracker,
)


def test_degraded_tracker_recovers_when_last_failure_is_ready():
    tracker = RobotInitTracker()
    tracker.register_components(["x_axis", "z_axis"])
    tracker.report("x_axis", "failed", reason="fault")
    tracker.report("z_axis", "ready")
    tracker.set_overall(OVERALL_DEGRADED)

    tracker.report("x_axis", "ready")

    assert tracker.failed_components() == []
    assert tracker.snapshot()["phase"] == OVERALL_READY


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




def test_failure_reasons_report_why_each_component_failed():
    tracker = RobotInitTracker()
    tracker.register_components(["rotate", "x_axis"])
    tracker.report("rotate", "failed", reason="Movement failed for motor 5 (status=FAULT)")
    tracker.report("x_axis", "ready")

    assert tracker.failure_reasons(tracker.failed_components()) == {
        "rotate": "Movement failed for motor 5 (status=FAULT)"
    }
