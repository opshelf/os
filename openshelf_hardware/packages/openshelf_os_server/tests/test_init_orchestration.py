from unittest.mock import MagicMock

import openshelf_os_server.core.robot as robot_module
from openshelf_os_server.core.init_progress import get_init_tracker


def _phase_for(component: str) -> str | None:
    snapshot = get_init_tracker().snapshot()
    for entry in snapshot["components"]:
        if entry["component"] == component:
            return entry["phase"]
    return None


def test_barcode_enabled_honors_env(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    assert robot_module._barcode_enabled() is True

    monkeypatch.setenv("OPENSHELF_DISABLE_BARCODE", "true")
    assert robot_module._barcode_enabled() is False


def test_report_scanner_status_ready_when_connected(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.get_scanner_status.return_value = {"connected": True, "enabled": True}

    robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "ready"


def test_report_scanner_status_failed_when_not_detected(monkeypatch):
    monkeypatch.delenv("OPENSHELF_DISABLE_BARCODE", raising=False)
    get_init_tracker().reset()
    worker = MagicMock()
    worker.get_scanner_status.return_value = {"connected": False, "enabled": True}

    robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") == "failed"


def test_report_scanner_status_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("OPENSHELF_DISABLE_BARCODE", "true")
    get_init_tracker().reset()
    worker = MagicMock()

    robot_module._report_scanner_status(worker)

    assert _phase_for("barcode") is None
    worker.get_scanner_status.assert_not_called()


def test_report_printer_status_failed_when_device_missing(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(robot_module.os.path, "exists", lambda _path: False)

    robot_module._report_printer_status()

    assert _phase_for("printer") == "failed"


def test_report_printer_status_ready_when_device_present(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: True)
    monkeypatch.setattr(robot_module.os.path, "exists", lambda _path: True)

    robot_module._report_printer_status()

    assert _phase_for("printer") == "ready"


def test_report_printer_status_skipped_when_disabled(monkeypatch):
    get_init_tracker().reset()
    monkeypatch.setattr(robot_module, "_printer_enabled", lambda: False)

    robot_module._report_printer_status()

    assert _phase_for("printer") is None
