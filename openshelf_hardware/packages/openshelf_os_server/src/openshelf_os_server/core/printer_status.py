from __future__ import annotations

import os
import select
import time
from dataclasses import dataclass

DLE = b"\x10"
EOT = b"\x04"
STATUS_QUERY = b"".join(DLE + EOT + bytes([status_type]) for status_type in range(1, 5))
DEFAULT_STATUS_TIMEOUT_S = 1.0


class PrinterStatusProbeError(RuntimeError):
    """Raised when the printer cannot provide a complete ESC/POS status."""


@dataclass(frozen=True)
class PrinterStatus:
    raw: tuple[int, int, int, int]
    offline: bool
    waiting_for_online_recovery: bool
    feed_button_active: bool
    cover_open: bool
    paper_out: bool
    paper_near_end: bool
    recoverable_error: bool
    cutter_error: bool
    unrecoverable_error: bool
    auto_recoverable_error: bool

    def failure_reason(self) -> str | None:
        reasons: list[str] = []
        if self.paper_out is True:
            reasons.append("paper out")
        if self.cover_open is True:
            reasons.append("cover open")
        if self.cutter_error is True:
            reasons.append("cutter error")
        if self.unrecoverable_error is True:
            reasons.append("unrecoverable printer error")
        if self.recoverable_error is True:
            reasons.append("recoverable printer error")
        if self.auto_recoverable_error is True:
            reasons.append("auto-recoverable printer error")
        if self.feed_button_active is True:
            reasons.append("paper feed button active")
        if self.waiting_for_online_recovery is True:
            reasons.append("waiting for online recovery")
        if self.offline is True and not reasons:
            reasons.append("printer offline")
        return "; ".join(reasons) if reasons else None


def decode_printer_status(response: bytes) -> PrinterStatus:
    if len(response) != 4:
        raise PrinterStatusProbeError(f"incomplete status response: received {len(response)} of 4 bytes")

    for value in response:
        if value & 0x93 != 0x12:
            raise PrinterStatusProbeError(f"invalid status response: {response.hex()}")

    printer, offline_cause, error_cause, paper = response
    return PrinterStatus(
        raw=(printer, offline_cause, error_cause, paper),
        offline=bool(printer & 0x08),
        waiting_for_online_recovery=bool(printer & 0x20),
        feed_button_active=bool(printer & 0x40 or offline_cause & 0x08),
        cover_open=bool(offline_cause & 0x04),
        paper_out=paper & 0x60 == 0x60 or bool(offline_cause & 0x20),
        paper_near_end=paper & 0x0C == 0x0C,
        recoverable_error=bool(error_cause & 0x04),
        cutter_error=bool(error_cause & 0x08),
        unrecoverable_error=bool(error_cause & 0x20),
        auto_recoverable_error=bool(error_cause & 0x40),
    )


def _write_status_query(file_descriptor: int, deadline: float) -> None:
    written = 0
    while written < len(STATUS_QUERY):
        try:
            count = os.write(file_descriptor, STATUS_QUERY[written:])
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PrinterStatusProbeError("status request timed out") from None
            _, writable, _ = select.select([], [file_descriptor], [], remaining)
            if not writable:
                raise PrinterStatusProbeError("status request timed out")
            continue
        if count <= 0:
            raise PrinterStatusProbeError("printer rejected status request")
        written += count


def _read_status_response(file_descriptor: int, deadline: float) -> bytes:
    response = bytearray()
    while len(response) < 4:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        readable, _, _ = select.select([file_descriptor], [], [], remaining)
        if not readable:
            break
        try:
            chunk = os.read(file_descriptor, 4 - len(response))
        except BlockingIOError:
            continue
        if not chunk:
            break
        response.extend(chunk)
    return bytes(response)


def probe_printer_status(
    device: str = "/dev/usb/lp0",
    *,
    timeout_s: float = DEFAULT_STATUS_TIMEOUT_S,
) -> PrinterStatus:
    if os.path.exists(device) is False:
        raise PrinterStatusProbeError(f"device not found: {device}")

    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(device, os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)
        deadline = time.monotonic() + timeout_s
        _write_status_query(file_descriptor, deadline)
        response = _read_status_response(file_descriptor, deadline)
        if not response:
            raise PrinterStatusProbeError("status response timed out")
        return decode_printer_status(response)
    except PrinterStatusProbeError:
        raise
    except OSError as exc:
        detail = exc.strerror or str(exc)
        raise PrinterStatusProbeError(f"unable to query {device}: {detail}") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
