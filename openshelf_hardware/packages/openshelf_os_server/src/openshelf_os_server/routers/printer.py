from __future__ import annotations

import asyncio
import errno
import logging
import os
import threading
from types import TracebackType
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
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
    code: str = Field(min_length=1, max_length=128, pattern=r"^[\x20-\x7E]+$")


class PrintLabelResponse(BaseModel):
    message: str
    code: str
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
        self._write_all(build_label_job(payload, feed_dots=feed_dots))


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
)
async def get_printer_config() -> PrinterFeedConfig:
    return PrinterFeedConfig(feed_dots=get_root_config().printer.feed_dots)


@router.put(
    "/config",
    response_model=PrinterFeedConfig,
    dependencies=[Depends(get_current_user), Depends(require_permissions("printer:print"))],
)
async def update_printer_config(request: PrinterFeedConfig) -> PrinterFeedConfig:
    config = get_root_config()
    config.printer.feed_dots = request.feed_dots
    save_root_config(config)
    return PrinterFeedConfig(feed_dots=config.printer.feed_dots)


@router.get(
    "/status",
    response_model=PrinterStatusResponse,
    dependencies=[Depends(get_current_user)],
)
async def get_printer_status() -> PrinterStatusResponse:
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
)
async def print_custom_label(request: PrintLabelRequest) -> PrintLabelResponse:
    return PrintLabelResponse(
        message="success",
        code=request.code,
        printed=await asyncio.to_thread(_print_payload, request.code),
    )


@router.post(
    "/{upca}",
    dependencies=[Depends(require_relay_or_permissions("printer:print"))],
)
async def print_label(upca: str) -> dict[str, str | bool]:
    if get_operating_mode() == OperatingMode.mock:
        if len(upca) not in {11, 12} or upca.isascii() is False or upca.isdecimal() is False:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="UPCA must contain 11 or 12 decimal digits",
            )
    return {
        "message": "success",
        "upca": upca,
        "printed": await asyncio.to_thread(_print_payload, upca),
    }
