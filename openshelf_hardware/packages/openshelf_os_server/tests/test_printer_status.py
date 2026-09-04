from unittest.mock import MagicMock

import pytest

import openshelf_os_server.core.printer_status as printer_status_module
from openshelf_os_server.core.printer_status import (
    PrinterStatusProbeError,
    decode_printer_status,
    probe_printer_status,
)


def test_decode_printer_status_reports_healthy_printer() -> None:
    status = decode_printer_status(bytes.fromhex("12121212"))

    assert status.failure_reason() is None
    assert status.offline is False
    assert status.paper_out is False
    assert status.paper_near_end is False


def test_decode_printer_status_reports_observed_paper_out_status() -> None:
    status = decode_printer_status(bytes.fromhex("1a721272"))

    assert status.offline is True
    assert status.paper_out is True
    assert status.cover_open is False
    assert status.failure_reason() == "paper out"


def test_decode_printer_status_reports_cover_and_error_conditions() -> None:
    status = decode_printer_status(bytes.fromhex("1a165e12"))

    assert status.cover_open is True
    assert status.recoverable_error is True
    assert status.cutter_error is True
    assert status.auto_recoverable_error is True
    assert status.failure_reason() == (
        "cover open; cutter error; recoverable printer error; auto-recoverable printer error"
    )


def test_decode_printer_status_allows_paper_near_end() -> None:
    status = decode_printer_status(bytes.fromhex("1212121e"))

    assert status.paper_near_end is True
    assert status.failure_reason() is None


@pytest.mark.parametrize("response", [b"", b"\x12\x12\x12"])
def test_decode_printer_status_rejects_incomplete_response(response: bytes) -> None:
    with pytest.raises(PrinterStatusProbeError, match="incomplete status response"):
        decode_printer_status(response)


def test_decode_printer_status_rejects_invalid_response() -> None:
    with pytest.raises(PrinterStatusProbeError, match="invalid status response"):
        decode_printer_status(b"ABCD")


def test_probe_printer_status_reports_missing_device(monkeypatch) -> None:
    monkeypatch.setattr(printer_status_module.os.path, "exists", MagicMock(return_value=False))

    with pytest.raises(PrinterStatusProbeError, match="device not found"):
        probe_printer_status("/dev/usb/lp0")


def test_probe_printer_status_reports_open_failure(monkeypatch) -> None:
    monkeypatch.setattr(printer_status_module.os.path, "exists", MagicMock(return_value=True))
    monkeypatch.setattr(
        printer_status_module.os,
        "open",
        MagicMock(side_effect=PermissionError(13, "Permission denied")),
    )

    with pytest.raises(PrinterStatusProbeError, match="unable to query /dev/usb/lp0: Permission denied"):
        probe_printer_status("/dev/usb/lp0")


def test_probe_printer_status_reports_response_timeout(monkeypatch) -> None:
    monkeypatch.setattr(printer_status_module.os.path, "exists", MagicMock(return_value=True))
    monkeypatch.setattr(printer_status_module.os, "open", MagicMock(return_value=42))
    monkeypatch.setattr(printer_status_module.os, "write", MagicMock(return_value=12))
    monkeypatch.setattr(printer_status_module.os, "close", MagicMock())
    monkeypatch.setattr(printer_status_module.select, "select", MagicMock(return_value=([], [], [])))

    with pytest.raises(PrinterStatusProbeError, match="status response timed out"):
        probe_printer_status("/dev/usb/lp0")

    printer_status_module.os.close.assert_called_once_with(42)


def test_probe_printer_status_reads_complete_response(monkeypatch) -> None:
    monkeypatch.setattr(printer_status_module.os.path, "exists", MagicMock(return_value=True))
    monkeypatch.setattr(printer_status_module.os, "open", MagicMock(return_value=42))
    monkeypatch.setattr(printer_status_module.os, "write", MagicMock(return_value=12))
    monkeypatch.setattr(printer_status_module.os, "read", MagicMock(return_value=bytes.fromhex("12121212")))
    monkeypatch.setattr(printer_status_module.os, "close", MagicMock())
    monkeypatch.setattr(printer_status_module.select, "select", MagicMock(return_value=([42], [], [])))

    status = probe_printer_status("/dev/usb/lp0")

    assert status.failure_reason() is None
    printer_status_module.os.write.assert_called_once_with(42, bytes.fromhex("100401100402100403100404"))
    printer_status_module.os.close.assert_called_once_with(42)
