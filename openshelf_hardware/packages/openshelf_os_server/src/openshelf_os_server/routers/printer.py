from __future__ import annotations

import errno
import logging
import os
from types import TracebackType
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status

from openshelf_os.operating_mode import OperatingMode

from openshelf_os_server.core.label_printer import build_label_job
from openshelf_os_server.routers.auth_deps import require_relay_or_permissions
from openshelf_os_server.settings import get_operating_mode, get_settings


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

    def print_label(self, payload: str) -> None:
        self._write_all(build_label_job(payload))


router = APIRouter(tags=["printer"])
printer = USBPrinter()


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
        return {"message": "success", "upca": upca, "printed": True}

    settings = get_settings()
    if settings.printer.enabled is False:
        logging.info("Printer disabled; skipping label print for %s", upca)
        return {"message": "success", "upca": upca, "printed": False}

    try:
        printer.print_label(upca)
    finally:
        printer.close()

    return {"message": "success", "upca": upca, "printed": True}
