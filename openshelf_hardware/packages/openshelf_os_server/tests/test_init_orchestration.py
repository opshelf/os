from unittest.mock import AsyncMock, MagicMock

import pytest

import openshelf_os_server.core.robot as robot_module
from openshelf_os_server.core.init_progress import get_init_tracker


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
