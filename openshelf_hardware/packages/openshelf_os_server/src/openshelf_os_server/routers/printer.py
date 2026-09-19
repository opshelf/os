from __future__ import annotations
from openshelf_os_server.documentation import response_docs

import asyncio
import errno
import logging
import os
import threading
from types import TracebackType
from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from openshelf_os.operating_mode import OperatingMode

from openshelf_os_server.core.label_printer import build_label_job
from openshelf_os_server.core.printer_status import (
    PrinterStatus,
    PrinterStatusProbeError,
    probe_printer_status,
)
from openshelf_os_server.routers.auth_deps import (
    get_current_user,
    require_permissions,
    require_relay_or_permissions,
)
from openshelf_os_server.settings import (
    get_operating_mode,
    get_root_config,
    get_settings,
    save_root_config,
)


class PrinterFeedConfig(BaseModel):
    feed_dots: int = Field(ge=0, le=128)


class PrinterStatusResponse(BaseModel):
    enabled: bool
    connected: bool
    healthy: bool
    simulated: bool = False
    error: str | None = None
    paper_out: bool = False
    paper_near_end: bool = False
    cover_open: bool = False
    cutter_error: bool = False
    recoverable_error: bool = False
    unrecoverable_error: bool = False
    auto_recoverable_error: bool = False


class PrintLabelRequest(BaseModel):
    barcode: str = Field(min_length=1, max_length=128, pattern=r"^[\x20-\x7E]+$")


class PrintLabelResponse(BaseModel):
    message: str
    barcode: str
    printed: bool


class USBPrinter:
    """ESC/POS direct-write printer for /dev/usb/lp0."""

    def __init__(self, device: str = "/dev/usb/lp0"):
        self.device = device
        self._fd: Optional[int] = None

    def open(self) -> None:
        if self._fd is not None:
            return
        self._fd = os.open(self.device, os.O_WRONLY | os.O_CLOEXEC)

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self) -> "USBPrinter":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _write_all(self, data: bytes) -> None:
        if self._fd is None:
            self.open()
        try:
            self._write_all_once(data)
        except OSError as e:
            if e.errno in (errno.EPIPE, errno.ENODEV, errno.EIO):
                self.close()
                self.open()
                self._write_all_once(data)
            else:
                raise

    def _write_all_once(self, data: bytes) -> None:
        assert self._fd is not None
        view = memoryview(data)
        total = 0
        while total < len(view):
            n = os.write(self._fd, view[total:])
            if n <= 0:
                raise OSError(errno.EIO, "short write to printer")
            total += n

    def print_label(self, payload: str, *, feed_dots: int) -> None:
        """Write a Code 128 label job with the configured feed distance to the printer."""
        self._write_all(build_label_job(payload, feed_dots=feed_dots))


class PrintBarcodeResponse(BaseModel):
    message: Literal["success"]
    barcode: str
    printed: bool


router = APIRouter(tags=["printer"])
printer = USBPrinter()
printer_io_lock = threading.Lock()


def _probe_status() -> PrinterStatus:
    with printer_io_lock:
        return probe_printer_status(printer.device)


@router.get(
    "/config",
    response_model=PrinterFeedConfig,
    dependencies=[Depends(get_current_user)],
    summary="Read printer feed settings",
    responses=response_docs(errors=(401,)),
)
async def get_printer_config() -> PrinterFeedConfig:
    """Read printer feed settings.

    Returns configured feed distance in printer dots. Does not print a label.
    """
    return PrinterFeedConfig(feed_dots=get_root_config().printer.feed_dots)


@router.put(
    "/config",
    response_model=PrinterFeedConfig,
    dependencies=[Depends(get_current_user), Depends(require_permissions("printer:print"))],
    summary="Set printer feed distance",
    responses=response_docs(errors=(401, 403)),
)
async def update_printer_config(request: PrinterFeedConfig) -> PrinterFeedConfig:
    """Set printer feed distance.

    Requires `printer:print`. Persists the feed distance used after printing. This updates
    configuration without printing a test label.
    """
    config = get_root_config()
    config.printer.feed_dots = request.feed_dots
    save_root_config(config)
    return PrinterFeedConfig(feed_dots=config.printer.feed_dots)


@router.get(
    "/status",
    response_model=PrinterStatusResponse,
    dependencies=[Depends(get_current_user)],
    summary="Read printer diagnostics",
    responses=response_docs(errors=(401,)),
)
async def get_printer_status() -> PrinterStatusResponse:
    """Read printer diagnostics.

    Reports connectivity, health, and printer error flags. A disconnected or unhealthy
    printer can still return HTTP `200`; inspect the body. Mock mode reports
    `simulated=true` and does not contact hardware.
    """
    settings = get_settings()
    if get_operating_mode() == OperatingMode.mock:
        return PrinterStatusResponse(
            enabled=settings.printer.enabled,
            connected=False,
            healthy=True,
            simulated=True,
        )

    try:
        current_status = await asyncio.to_thread(_probe_status)
    except PrinterStatusProbeError as exc:
        return PrinterStatusResponse(
            enabled=settings.printer.enabled,
            connected=False,
            healthy=False,
            error=str(exc),
        )

    failure_reason = current_status.failure_reason()
    return PrinterStatusResponse(
        enabled=settings.printer.enabled,
        connected=True,
        healthy=failure_reason is None,
        error=failure_reason,
        paper_out=current_status.paper_out,
        paper_near_end=current_status.paper_near_end,
        cover_open=current_status.cover_open,
        cutter_error=current_status.cutter_error,
        recoverable_error=current_status.recoverable_error,
        unrecoverable_error=current_status.unrecoverable_error,
        auto_recoverable_error=current_status.auto_recoverable_error,
    )


def _print_payload(payload: str) -> bool:
    if get_operating_mode() == OperatingMode.mock:
        return True

    settings = get_settings()
    if settings.printer.enabled is False:
        logging.info("Printer disabled; skipping label print for %s", payload)
        return False

    with printer_io_lock:
        try:
            printer.print_label(payload, feed_dots=settings.printer.feed_dots)
        finally:
            printer.close()
    return True


@router.post(
    "",
    response_model=PrintLabelResponse,
    dependencies=[Depends(require_relay_or_permissions("printer:print"))],
    summary="Print a barcode label",
    responses=response_docs(errors=(401, 403)),
)
async def print_custom_label(request: PrintLabelRequest) -> PrintLabelResponse:
    """Print a barcode label.

    Requires `printer:print` or the relay service credential. Prints the requested barcode and
    returns whether printing was accepted. Mock mode validates input and simulates printing
    without a physical artifact. This operation has no idempotency key; a retry may print
    another label.
    """
    return PrintLabelResponse(
        message="success",
        barcode=request.barcode,
        printed=await asyncio.to_thread(_print_payload, request.barcode),
    )


@router.post(
    "/{barcode}",
    dependencies=[Depends(require_relay_or_permissions("printer:print"))],
    summary="Print a barcode label",
    responses=response_docs(model=PrintBarcodeResponse, errors=(401, 403, 422)),
)
async def print_label(barcode: str) -> dict[str, str | bool]:
    """Print a barcode label.

    Requires `printer:print` or the relay service credential. The supplied value is printed
    as a Code 128 label, so any non-empty ASCII barcode is accepted; mock mode returns
    `printed=true` without a physical label. Returns the supplied barcode and `printed` flag
    (`false` when printing is disabled). Does not create inventory.
    """
    return {
        "message": "success",
        "barcode": barcode,
        "printed": await asyncio.to_thread(_print_payload, barcode),
    }
