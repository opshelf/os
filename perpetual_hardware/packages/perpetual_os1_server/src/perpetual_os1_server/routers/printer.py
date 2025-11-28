from __future__ import annotations

import errno
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, status

from perpetual_os1_server.core.printer import LabelConfiguration, create_label
from perpetual_os1_server.routers.auth_deps import require_permissions
from perpetual_os1_server.settings import get_settings

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

    def __exit__(self, exc_type, exc, tb) -> None:
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
    #     # print(upca)
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

def _asset_paths() -> Tuple[str, str, str]:
    base_dir = Path(__file__).resolve().parents[3]
    logo_path = base_dir / "logo.png"
    barcode_path = base_dir / "barcode.png"
    output_path = base_dir / "label.png"
    return str(logo_path), str(barcode_path), str(output_path)


def _build_attributes(upca: str) -> List[Tuple[str, str]]:
    return [
        ("UPCA:", upca),
    ]


def _build_label_configuration(upca: str) -> LabelConfiguration:
    logo_path, barcode_path, output_path = _asset_paths()
    attributes = _build_attributes(upca)
    return LabelConfiguration(
        upca=upca,
        logo_path=logo_path,
        barcode_path=barcode_path,
        output_path=output_path,
        attributes=attributes,
    )


# def _dispatch_to_printer(output_path: str) -> None:
#     command = [
#         "lpr",
#         "-o",
#         "media=Custom3x2",
#         "-o",
#         "fit-to-page",
#         "-o",
#         "orientation-requested=1",
#     ]
#     settings = get_settings()
#     queue = settings.printer.queue or os.getenv("PRINTER")
#     if queue:
#         command.extend(["-P", queue])

#     command.append(output_path)

#     try:
#         result = subprocess.run(command, capture_output=True, text=True, check=False)
#     except FileNotFoundError as exc:
#         logging.error("Print command not found: %s", exc)
#         raise HTTPException(
#             status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
#             detail="Printer command unavailable",
#         ) from exc

#     if result.returncode != 0:
        # logging.error("Printer command failed: %s", result.stderr.strip())
        # if "No default destination" in (result.stderr or ""):
        #     raise HTTPException(
        #         status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        #         detail="Printer command failed: no default destination (configure printer queue)",
        #     )
        # raise HTTPException(
        #     status_code=status.HTTP_502_BAD_GATEWAY,
        #     detail="Printer command failed",
        # )


@router.post(
    "/{upca}",
    dependencies=[Depends(require_permissions("printer:print"))],
)
async def print_label(upca: str) -> dict[str, str | bool]:
    settings = get_settings()
    config = _build_label_configuration(upca)

    try:
        create_label(config)
    except Exception as exc:
        logging.exception("Failed to generate label for %s", upca)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate label",
        ) from exc

    printed = False
    if settings.printer.enabled:
        # _dispatch_to_printer(config.output_path)
        printer.print_barcode(sym="UPC-A", payload=upca)
        printed = True
    else:
        logging.info("Printer disabled; generated label at %s", config.output_path)

    return {"message": "success", "upca": upca, "printed": printed or settings.printer.enabled}
