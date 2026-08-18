from __future__ import annotations

import errno
import logging
import os
from types import TracebackType
from typing import Optional, Type

from fastapi import APIRouter, Depends, HTTPException, status

from openshelf_os.operating_mode import OperatingMode

from openshelf_os_server.routers.auth_deps import require_permissions
from openshelf_os_server.settings import get_operating_mode, get_settings

ESC = b"\x1b"
GS  = b"\x1d"

HRI_MAP = {"none":0, "above":1, "below":2, "both":3}

# GS k (mode B) symbology map
SYMS = {
    "UPC-A":65, "UPCA":65,
    "UPC-E":66, "UPCE":66,
    "EAN13":67, "JAN13":67,
    "EAN8":68,  "JAN8":68,
    "CODE39":69, "CODE-39":69,
    "ITF":70, "I2OF5":70, "INTERLEAVED2OF5":70,
    "CODABAR":71, "NW-7":71,
    "CODE93":72,
    "CODE128":73,
    "UCC/EAN128":74, "GS1-128":74, "EAN128":74,
}

# def _calculate_upc_check_digit(digits: List[str]) -> int:
#     odd_sum = sum(int(digits[i]) for i in range(0, 11, 2))
#     even_sum = sum(int(digits[i]) for i in range(1, 11, 2))
#     total = odd_sum * 3 + even_sum
#     return (10 - (total % 10)) % 10


# def generate_random_upca() -> str:
#     digits = [str(random.randint(0, 9)) for _ in range(11)]
#     check_digit = _calculate_upc_check_digit(digits)
#     return "".join(digits) + str(check_digit)


class USBPrinter:
    """
    Minimal ESC/POS direct-write printer for /dev/usb/lp0
    Designed to be robust inside Docker containers.
    """
    def __init__(self, device: str = "/dev/usb/lp0"):
        self.device = device
        self._fd: Optional[int] = None

    # --------- lifecycle ----------
    def open(self) -> None:
        if self._fd is not None:
            return
        # O_SYNC to push bytes promptly; no buffering
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
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType]
    ) -> None:
        self.close()

    # --------- low-level write with one automatic reopen ----------
    def _write_all(self, data: bytes) -> None:
        if self._fd is None:
            self.open()
        try:
            self._write_all_once(data)
        except OSError as e:
            # Handle device disappearing mid-job in containers
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

    # --------- ESC/POS helpers ----------
    def init(self) -> None:
        self._write_all(ESC + b"@")

    def set_hri(self, where: str = "none") -> None:
        n = HRI_MAP.get(where.lower(), 0)
        self._write_all(GS + b"H" + bytes([n]))

    def set_height(self, dots: int = 96) -> None:
        if not (1 <= dots <= 255):
            raise ValueError("height must be 1..255 dots")
        self._write_all(GS + b"h" + bytes([dots]))

    def set_width(self, module_dots: int = 3) -> None:
        if not (1 <= module_dots <= 6):
            raise ValueError("width must be 1..6 dots")
        self._write_all(GS + b"w" + bytes([module_dots]))

    def _build_barcode_cmd(self, sym: str, data: bytes) -> bytes:
        m = SYMS.get(sym.upper())
        if m is None:
            raise ValueError(f"unsupported symbology: {sym}")
        if not (1 <= len(data) <= 255):
            raise ValueError("data length must be 1..255")
        # GS k m n [data]
        return GS + b"k" + bytes([m, len(data)]) + data

    # --------- public API ----------
    def print_barcode(
        self,
        sym: str = "CODE128",
        payload: str = "beblet",
        *,
        height: int = 96,
        width: int = 3,
        hri: str = "none",
        raw: bool = False,
        feed_after: int = 2,
    ) -> None:
        data = payload.encode("ascii", "strict")
        # Helpful default for Code128: select set B unless raw requested
        if sym.upper() == "CODE128" and not raw:
            data = b"{B" + data

        # Build & send
        self.init()
        self.set_hri(hri)
        self.set_height(height)
        self.set_width(width)
        self._write_all(self._build_barcode_cmd(sym, data))
        if feed_after > 0:
            self._write_all(b"\n" * feed_after)

    # def print_random_upca(
    #     self,
    #     *,
    #     height: int = 96,
    #     width: int = 3,
    #     hri: str = "none",
    #     feed_after: int = 2,
    # ) -> str:
    #     """Generate a random UPC-A code and print it."""
    #     upca = generate_random_upca()
    #     self.print_barcode(
    #         sym="UPC-A",
    #         payload=upca,
    #         height=height,
    #         width=width,
    #         hri=hri,
    #         feed_after=feed_after,
    #     )
    #     return upca

###############################

router = APIRouter(tags=["printer"])
printer = USBPrinter()

@router.post(
    "/{upca}",
    dependencies=[Depends(require_permissions("printer:print"))],
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
        printer.print_barcode(sym="UPC-A", payload=upca)
    finally:
        printer.close()

    return {"message": "success", "upca": upca, "printed": True}
