"""
USB printer component driver.

This module provides a small asynchronous-friendly driver that can talk to a
USB-connected receipt/label printer exposed as a character device (e.g.
/dev/usb/lp0). It focuses on reliably sending UTF-8 text payloads while allowing
the caller to adjust the amount of trailing whitespace (spaces and/or blank
lines) that is appended to each print job.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from pathlib import Path

from hardware.exceptions import CommunicationError

log = logging.getLogger(__name__)


class PrinterError(CommunicationError):
    """Base class for printer-related errors."""


class PrinterNotConnectedError(PrinterError):
    """Raised when an operation requires an active USB connection."""


class USBPrinter:
    """
    Minimal USB printer driver that streams bytes to a character device.

    Parameters:
        device_path: Path to the printer device (default /dev/usb/lp0).
        encoding:  Text encoding used when sending strings.
        default_trailing_spaces: Number of spaces appended to every print job.
        default_trailing_newlines: Number of newline characters appended.
    """

    def __init__(
        self,
        device_path: str = "/dev/usb/lp0",
        encoding: str = "utf-8",
        default_trailing_spaces: int = 0,
        default_trailing_newlines: int = 0,
    ) -> None:
        self.device_path = Path(device_path)
        self.encoding = encoding
        self._default_trailing_spaces = max(0, int(default_trailing_spaces))
        self._default_trailing_newlines = max(0, int(default_trailing_newlines))

        self._device_file: Optional[object] = None
        self._lock = threading.Lock()

    # --------------------------------------------------------------------- #
    # Public configuration helpers

    def configure_trailing_whitespace(
        self,
        *,
        trailing_spaces: Optional[int] = None,
        trailing_newlines: Optional[int] = None,
    ) -> None:
        """
        Update the default whitespace appended to each print job.

        Args:
            trailing_spaces:  Override default trailing spaces (>= 0).
            trailing_newlines: Override default trailing newline characters (>= 0).
        """
        if trailing_spaces is not None:
            if trailing_spaces < 0:
                raise ValueError("trailing_spaces must be >= 0")
            self._default_trailing_spaces = int(trailing_spaces)
        if trailing_newlines is not None:
            if trailing_newlines < 0:
                raise ValueError("trailing_newlines must be >= 0")
            self._default_trailing_newlines = int(trailing_newlines)

    # --------------------------------------------------------------------- #
    # Connection management

    def connect(self) -> None:
        """
        Locate and open the printer device.

        Raises:
            PrinterError: If the device file cannot be opened.
        """
        if self._device_file is not None:
            log.debug("Printer device already open at %s", self.device_path)
            return

        if not self.device_path.exists():
            raise PrinterError(f"Printer device {self.device_path} does not exist")

        try:
            # buffering=0 ensures writes are pushed immediately.
            self._device_file = self.device_path.open("wb", buffering=0)
        except OSError as exc:
            raise PrinterError(f"Failed to open printer device {self.device_path}: {exc}") from exc

        log.debug("Printer device %s opened", self.device_path)

    def disconnect(self) -> None:
        """Close printer device handle."""
        if self._device_file is None:
            return

        with self._lock:
            try:
                self._device_file.close()
            except Exception:
                log.exception("Failed to close printer device %s cleanly", self.device_path)
            finally:
                self._device_file = None
                log.debug("Printer device %s closed", self.device_path)

    # --------------------------------------------------------------------- #
    # Printing API

    def print_text(
        self,
        text: str,
        *,
        trailing_spaces: Optional[int] = None,
        trailing_newlines: Optional[int] = None,
    ) -> None:
        """
        Print the given text string with optional trailing whitespace.

        Args:
            text: The string payload to print.
            trailing_spaces: Optional override for spaces appended after text.
            trailing_newlines: Optional override for newline characters appended.
        """
        payload = self._prepare_payload(
            text,
            trailing_spaces=trailing_spaces,
            trailing_newlines=trailing_newlines,
        )
        self.print_raw(payload)

    def print_raw(self, data: bytes) -> None:
        """
        Send raw bytes to the printer.

        Raises:
            PrinterNotConnectedError: If the USB device is not connected.
            PrinterError: On write failures.
        """
        if not data:
            return
        if self._device_file is None:
            raise PrinterNotConnectedError("Printer is not connected")

        with self._lock:
            try:
                self._device_file.write(data)
                self._device_file.flush()
            except OSError as exc:
                raise PrinterError(f"Failed to write to printer device {self.device_path}: {exc}") from exc

    # --------------------------------------------------------------------- #
    # Context manager helpers

    def __enter__(self) -> "USBPrinter":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    # --------------------------------------------------------------------- #
    # Internal helpers

    def _prepare_payload(
        self,
        text: str,
        *,
        trailing_spaces: Optional[int],
        trailing_newlines: Optional[int],
    ) -> bytes:
        spaces = self._default_trailing_spaces if trailing_spaces is None else max(0, int(trailing_spaces))
        newlines = self._default_trailing_newlines if trailing_newlines is None else max(0, int(trailing_newlines))

        whitespace = " " * spaces + ("\n" * newlines)
        payload_str = f"{text}{whitespace}"
        try:
            return payload_str.encode(self.encoding, errors="replace")
        except LookupError as exc:
            raise PrinterError(f"Unknown printer encoding '{self.encoding}'") from exc
