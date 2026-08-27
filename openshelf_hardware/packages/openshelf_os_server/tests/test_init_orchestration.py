from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest

import openshelf_os_server.core.robot as robot_module
from openshelf_os_server.core.init_progress import OVERALL_READY, get_init_tracker
from openshelf_os_server.core.printer_status import PrinterStatus, PrinterStatusProbeError


def _phase_for(component: str) -> str | None:
    snapshot = get_init_tracker().snapshot()
    for entry in snapshot["components"]:
        if entry["component"] == component:
            return entry["phase"]
    return None


def _reason_for(component: str) -> str | None:
    snapshot = get_init_tracker().snapshot()
    for entry in snapshot["components"]:
        if entry["component"] == component:
            return entry["reason"]
    return None


def test_barcode_enabled_honors_env(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    assert robot_module._barcode_enabled() is True

    monkeypatch.setenv("OPENSHELF_DISABLE_BARCODE", "true")
    assert robot_module._barcode_enabled() is False


@pytest.mark.anyio
async def test_report_scanner_status_ready_when_connected(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=True)

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "ready"


@pytest.mark.anyio
async def test_report_scanner_status_failed_when_not_detected(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=False)
    worker.describe_scanner_failure = MagicMock(return_value="scanner not detected")

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "failed"
    assert _reason_for("barcode") == "scanner not detected"


@pytest.mark.anyio
async def test_report_scanner_status_failed_reports_hid_mode(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=False)
    worker.describe_scanner_failure = MagicMock(
        return_value="scanner is in usb hid keyboard mode; switch it to usb-com (serial) mode"
    )

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "failed"
    assert _reason_for("barcode") == (
        "scanner is in usb hid keyboard mode; switch it to usb-com (serial) mode"
    )


@pytest.mark.anyio
async def test_report_scanner_status_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("OPENSHELF_DISABLE_BARCODE", "true")
    get_init_tracker().reset()
    worker = MagicMock()
    worker.wait_scanner_connected = AsyncMock(return_value=True)

    await robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") is None
    worker.wait_scanner_connected.assert_not_called()


def test_robot_health_is_ok_when_only_camera_component_failed(monkeypatch):
    tracker = get_init_tracker()
    tracker.reset()
    tracker.register_components(["command_worker"])
    tracker.report("command_worker", "ready")
    tracker.register_components(["tool_camera"], degrade=False)
    tracker.set_overall(OVERALL_READY)
    tracker.report("tool_camera", "failed", reason="no frames within 15s")

    monkeypatch.setattr(robot_module, "robot_worker", MagicMock())

    health = robot_module.get_robot_health()

    assert health["status"] == "ok"
    assert health["phase"] == OVERALL_READY
    assert health["failed_components"] == []


def test_robot_health_is_degraded_when_motor_component_failed(monkeypatch):
    tracker = get_init_tracker()
    tracker.reset()
    tracker.register_components(["command_worker", "y_axis"])
    tracker.report("command_worker", "ready")
    tracker.report("y_axis", "failed", reason="fault")
    tracker.set_overall("degraded")

    monkeypatch.setattr(robot_module, "robot_worker", MagicMock())

    health = robot_module.get_robot_health()

    assert health["status"] == "degraded"
    assert health["failed_components"] == ["y_axis"]


def _printer_status(**overrides: bool) -> PrinterStatus:
    status = PrinterStatus(
        raw=(0x12, 0x12, 0x12, 0x12),
        offline=False,
        waiting_for_online_recovery=False,
        feed_button_active=False,
        cover_open=False,
        paper_out=False,
        paper_near_end=False,
        recoverable_error=False,
        cutter_error=False,
        unrecoverable_error=False,
        auto_recoverable_error=False,
    )
    return replace(status, **overrides)


def test_report_printer_status_failed_when_probe_fails(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(
        robot_module,
        "probe_printer_status",
        MagicMock(side_effect=PrinterStatusProbeError("device not found: /dev/usb/lp0")),
    )

    robot_module._report_printer_status()

    assert _phase_for("printer") == "failed"
    assert _reason_for("printer") == "device not found: /dev/usb/lp0"


def test_report_printer_status_ready_when_probe_is_healthy(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    probe = MagicMock(return_value=_printer_status())
    monkeypatch.setattr(robot_module, "probe_printer_status", probe)

    robot_module._report_printer_status()

    assert _phase_for("printer") == "ready"
    probe.assert_called_once_with("/dev/usb/lp0")


@pytest.mark.parametrize(
    ("condition", "reason"),
    [
        ({"paper_out": True, "offline": True}, "paper out"),
        ({"cover_open": True, "offline": True}, "cover open"),
        ({"cutter_error": True, "offline": True}, "cutter error"),
        ({"recoverable_error": True, "offline": True}, "recoverable printer error"),
        ({"unrecoverable_error": True, "offline": True}, "unrecoverable printer error"),
        ({"auto_recoverable_error": True, "offline": True}, "auto-recoverable printer error"),
        ({"offline": True}, "printer offline"),
    ],
)
def test_report_printer_status_failed_for_unhealthy_status(monkeypatch, condition, reason):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(robot_module, "probe_printer_status", MagicMock(return_value=_printer_status(**condition)))

    robot_module._report_printer_status()

    assert _phase_for("printer") == "failed"
    assert _reason_for("printer") == reason


def test_report_printer_status_ready_with_near_end_detail(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(
        robot_module,
        "probe_printer_status",
        MagicMock(return_value=_printer_status(paper_near_end=True)),
    )

    robot_module._report_printer_status()

    snapshot = get_init_tracker().snapshot()
    printer = next(entry for entry in snapshot["components"] if entry["component"] == "printer")
    assert printer["phase"] == "ready"
    assert printer["detail"] == "/dev/usb/lp0 (paper near end)"


def test_report_printer_status_skipped_when_disabled(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: False)

    robot_module._report_printer_status()

    assert _phase_for("printer") is None
