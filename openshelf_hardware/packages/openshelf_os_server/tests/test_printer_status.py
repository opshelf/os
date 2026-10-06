
import pytest

from openshelf_os_server.core.printer_status import (
    PrinterStatusProbeError,
    decode_printer_status,
)


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


@pytest.mark.parametrize("response", [b"", b"\x12\x12\x12"])
def test_decode_printer_status_rejects_incomplete_response(response: bytes) -> None:
    with pytest.raises(PrinterStatusProbeError, match="incomplete status response"):
        decode_printer_status(response)


def test_decode_printer_status_rejects_invalid_response() -> None:
    with pytest.raises(PrinterStatusProbeError, match="invalid status response"):
        decode_printer_status(b"ABCD")

