#!/usr/bin/env python3
from __future__ import annotations
import argparse, os, sys, errno
from typing import Optional

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

# ---------- CLI wrapper ----------
def main():
    ap = argparse.ArgumentParser(description="Print a 1D barcode on an ESC/POS USB printer")
    ap.add_argument("--device", default="/dev/usb/lp0")
    ap.add_argument("--sym", default="CODE128",
                    help="CODE128, CODE39, EAN13, EAN8, ITF, etc.")
    ap.add_argument("--payload", default="beblet", help="ASCII barcode data")
    ap.add_argument("--height", type=int, default=96, help="1..255 dots")
    ap.add_argument("--width", type=int, default=3, help="module width 1..6 dots")
    ap.add_argument("--hri", choices=["none","above","below","both"], default="none")
    ap.add_argument("--raw", action="store_true", help="Do not add {B for CODE128")
    ap.add_argument("--feed", type=int, default=2, help="line feeds after print")
    args = ap.parse_args()

    try:
        with USBPrinter(args.device) as p:
            p.print_barcode(
                args.sym,
                args.payload,
                height=args.height,
                width=args.width,
                hri=args.hri,
                raw=args.raw,
                feed_after=args.feed,
            )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()
