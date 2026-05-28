from __future__ import annotations
import errno
import logging
import os
import random
import time

from typing import Optional, List
from hardware.exceptions import PrinterConnectionError

ESC = b"\x1b"
GS = b"\x1d"

DEFAULT_INIT_DELAY = 0.1
DEFAULT_COMMAND_DELAY = 0.02
DEFAULT_PRINT_DENSITY = 64

HRI_MAP = {"none": 0, "above": 1, "below": 2, "both": 3}

SYMS = {
    "UPC-A": 65, "UPCA": 65,
    "UPC-E": 66, "UPCE": 66,
    "EAN13": 67, "JAN13": 67,
    "EAN8": 68,  "JAN8": 68,
    "CODE39": 69, "CODE-39": 69,
    "ITF": 70, "I2OF5": 70, "INTERLEAVED2OF5": 70,
    "CODABAR": 71, "NW-7": 71,
    "CODE93": 72,
    "CODE128": 73,
    "UCC/EAN128": 74, "GS1-128": 74, "EAN128": 74,
}

logger = logging.getLogger(__name__).getChild("printer")


def _calculate_upc_check_digit(digits: List[str]) -> int:
    odd_sum = sum(int(digits[i]) for i in range(0, 11, 2))
    even_sum = sum(int(digits[i]) for i in range(1, 11, 2))
    total = odd_sum * 3 + even_sum
    return (10 - (total % 10)) % 10


def generate_random_upca() -> str:
    digits = [str(random.randint(0, 9)) for _ in range(11)]
    check_digit = _calculate_upc_check_digit(digits)
    return "".join(digits) + str(check_digit)


class USBPrinter:
    def __init__(
        self,
        device: str = "/dev/usb/lp0",
        init_delay: float = DEFAULT_INIT_DELAY,
        command_delay: float = DEFAULT_COMMAND_DELAY,
    ):
        self.device = device
        self.init_delay = init_delay
        self.command_delay = command_delay
        self._fd: Optional[int] = None
        self._write_count = 0
        self._last_write_time: Optional[float] = None

    def start(self) -> None:
        if self._fd is not None:
            return

        self._fd = os.open(self.device, os.O_WRONLY | os.O_SYNC | os.O_CLOEXEC)
        self._write_count = 0
        self._last_write_time = None

        logger.info(
            "Printer device opened",
            extra={"device": self.device, "flags": "O_WRONLY|O_SYNC|O_CLOEXEC"},
        )

    def stop(self) -> None:
        if self._fd is not None:
            write_count = self._write_count

            try:
                os.close(self._fd)
            finally:
                self._fd = None
                self._write_count = 0
                self._last_write_time = None

                logger.info(
                    "Printer device closed",
                    extra={"device": self.device, "total_writes": write_count},
                )

    def __enter__(self) -> "USBPrinter":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    async def __aenter__(self) -> "USBPrinter":
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _wait_command_delay(self) -> None:
        if self._last_write_time is not None and self.command_delay > 0:
            elapsed = time.monotonic() - self._last_write_time
            remaining = self.command_delay - elapsed

            if remaining > 0:
                time.sleep(remaining)

    def _write_all(self, data: bytes, description: str = "data") -> None:
        if self._fd is None:
            raise PrinterConnectionError(
                f"Printer not started (device {self.device})"
            )

        self._wait_command_delay()

        try:
            self._write_all_once(data, description)
        except OSError as e:
            if e.errno in (errno.EPIPE, errno.ENODEV, errno.EIO):
                logger.warning(
                    "Printer write failed; attempting reopen",
                    extra={
                        "device": self.device,
                        "errno": e.errno,
                        "description": description,
                        "data_len": len(data),
                    },
                )

                self.stop()
                time.sleep(0.2)
                self.start()
                self._write_all_once(data, description)
            else:
                logger.error(
                    "Printer write failed permanently",
                    extra={
                        "device": self.device,
                        "errno": e.errno,
                        "description": description,
                    },
                    exc_info=True,
                )

                raise

    def _write_all_once(self, data: bytes, description: str = "data") -> None:
        assert self._fd is not None

        view = memoryview(data)
        total = 0

        while total < len(view):
            n = os.write(self._fd, view[total:])

            if n <= 0:
                logger.error(
                    "Short write to printer",
                    extra={
                        "device": self.device,
                        "expected": len(view),
                        "written": total,
                        "description": description,
                    },
                )

                raise OSError(errno.EIO, "short write to printer")

            total += n

        self._write_count += 1
        self._last_write_time = time.monotonic()

    def init(self) -> None:
        self._write_all(ESC + b"@", description="ESC @ init")
        time.sleep(self.init_delay)

    def set_print_density(self, density: int = DEFAULT_PRINT_DENSITY) -> None:
        if not (0 <= density <= 255):
            raise ValueError("density must be 0..255")

        self._write_all(GS + b"E" + bytes([density]), description=f"GS E {density} density")

    def set_hri(self, where: str = "none") -> None:
        n = HRI_MAP.get(where.lower(), 0)
        self._write_all(GS + b"H" + bytes([n]), description=f"GS H {n} HRI")

    def set_height(self, dots: int = 96) -> None:
        if not (1 <= dots <= 255):
            raise ValueError("height must be 1..255 dots")

        self._write_all(GS + b"h" + bytes([dots]), description=f"GS h {dots} height")

    def set_width(self, module_dots: int = 3) -> None:
        if not (1 <= module_dots <= 6):
            raise ValueError("width must be 1..6 dots")

        self._write_all(GS + b"w" + bytes([module_dots]), description=f"GS w {module_dots} width")

    def _build_barcode_cmd(self, sym: str, data: bytes) -> bytes:
        m = SYMS.get(sym.upper())

        if m is None:
            raise ValueError(f"unsupported symbology: {sym}")

        if not (1 <= len(data) <= 255):
            raise ValueError("data length must be 1..255")

        return GS + b"k" + bytes([m, len(data)]) + data

    def print_barcode(
        self,
        sym: str = "CODE128",
        payload: str = "beblet",
        *,
        height: int = 72,
        width: int = 2,
        hri: str = "none",
        raw: bool = False,
        feed_after: int = 2,
        density: int = DEFAULT_PRINT_DENSITY,
    ) -> None:
        start_time = time.monotonic()

        logger.info(
            "Starting barcode print",
            extra={
                "device": self.device,
                "symbology": sym,
                "payload": payload,
                "height": height,
                "width": width,
                "density": density,
            },
        )

        data = payload.encode("ascii", "strict")

        if sym.upper() == "CODE128" and raw is False:
            data = b"{B" + data

        self.init()
        self.set_print_density(density)
        self.set_hri(hri)
        self.set_height(height)
        self.set_width(width)
        self._write_all(self._build_barcode_cmd(sym, data), description=f"GS k barcode {sym}")

        if feed_after > 0:
            self._write_all(b"\n" * feed_after, description="line feed")

        elapsed_ms = (time.monotonic() - start_time) * 1000

        logger.info(
            "Completed barcode print",
            extra={
                "device": self.device,
                "symbology": sym,
                "payload_len": len(payload),
                "elapsed_ms": round(elapsed_ms, 1),
                "total_writes": self._write_count,
            },
        )

    def print_random_upca(
        self,
        *,
        height: int = 72,
        width: int = 2,
        hri: str = "none",
        feed_after: int = 2,
        density: int = DEFAULT_PRINT_DENSITY,
    ) -> str:
        upca = generate_random_upca()

        logger.info("Generated random UPC-A", extra={"device": self.device, "upca": upca})

        self.print_barcode(
            sym="UPC-A",
            payload=upca,
            height=height,
            width=width,
            hri=hri,
            feed_after=feed_after,
            density=density,
        )

        return upca
