"""
USB printer component driver.

This module provides a small asynchronous-friendly driver that can talk to a
USB-connected receipt/label printer via PyUSB.  It focuses on reliably sending
UTF-8 text payloads while allowing the caller to adjust the amount of trailing
whitespace (spaces and/or blank lines) that is appended to each print job.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

try:
    import usb.core  # type: ignore
    import usb.util  # type: ignore
except ImportError:  # pragma: no cover - handled by runtime checks
    usb = None  # type: ignore

from hardware.exceptions import CommunicationError

log = logging.getLogger(__name__)


class PrinterError(CommunicationError):
    """Base class for printer-related errors."""


class PrinterNotConnectedError(PrinterError):
    """Raised when an operation requires an active USB connection."""


class USBPrinter:
    """
    Minimal USB printer driver using PyUSB.

    Parameters:
        vendor_id:  USB vendor ID of the printer.
        product_id: USB product ID of the printer.
        interface:  Interface index to claim (default 0).
        out_endpoint: Optional endpoint address for output; auto-detected if None.
        timeout_ms: Write timeout in milliseconds.
        encoding:  Text encoding used when sending strings.
        default_trailing_spaces: Number of spaces appended to every print job.
        default_trailing_newlines: Number of newline characters appended.
    """

    def __init__(
        self,
        vendor_id: int,
        product_id: int,
        interface: int = 0,
        out_endpoint: Optional[int] = None,
        timeout_ms: int = 2000,
        encoding: str = "utf-8",
        default_trailing_spaces: int = 0,
        default_trailing_newlines: int = 0,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.interface = interface
        self.out_endpoint = out_endpoint
        self.timeout_ms = timeout_ms
        self.encoding = encoding
        self._default_trailing_spaces = max(0, int(default_trailing_spaces))
        self._default_trailing_newlines = max(0, int(default_trailing_newlines))

        self._device: Optional["usb.core.Device"] = None  # type: ignore[name-defined]
        self._endpoint: Optional["usb.core.Endpoint"] = None  # type: ignore[name-defined]
        self._lock = threading.Lock()
        self._claimed = False

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
        Locate and claim the USB printer.

        Raises:
            PrinterError: If PyUSB is missing, the device cannot be found,
                          or the interface/endpoint cannot be claimed.
        """
        self._ensure_usb_available()
        if self._device is not None:
            log.debug("USB printer already connected")
            return

        device = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)  # type: ignore[name-defined]
        if device is None:
            raise PrinterError(
                f"USB printer {self.vendor_id:04x}:{self.product_id:04x} not found"
            )

        try:
            if device.is_kernel_driver_active(self.interface):
                device.detach_kernel_driver(self.interface)
        except (AttributeError, NotImplementedError):
            pass  # Some backends do not implement kernel driver checks
        except usb.core.USBError as exc:  # type: ignore[name-defined]
            raise PrinterError(f"Failed to detach kernel driver: {exc}") from exc

        try:
            device.set_configuration()
            cfg = device.get_active_configuration()
            intf = cfg[(self.interface, 0)]
            endpoint = self._resolve_endpoint(intf)
            usb.util.claim_interface(device, self.interface)  # type: ignore[name-defined]
        except usb.core.USBError as exc:  # type: ignore[name-defined]
            try:
                usb.util.release_interface(device, self.interface)  # type: ignore[name-defined]
            except Exception:
                pass
            raise PrinterError(f"Failed to initialize printer interface: {exc}") from exc

        self._device = device
        self._endpoint = endpoint
        self._claimed = True
        log.debug(
            "USB printer connected (vid=0x%04x pid=0x%04x endpoint=0x%02x)",
            self.vendor_id,
            self.product_id,
            endpoint.bEndpointAddress,
        )

    def disconnect(self) -> None:
        """Release USB resources."""
        device = self._device
        if device is None:
            return

        with self._lock:
            if self._claimed:
                try:
                    usb.util.release_interface(device, self.interface)  # type: ignore[name-defined]
                except Exception:
                    pass
                try:
                    device.attach_kernel_driver(self.interface)  # type: ignore[name-defined]
                except (usb.core.USBError, NotImplementedError):  # type: ignore[name-defined]
                    pass

            usb.util.dispose_resources(device)  # type: ignore[name-defined]
            self._claimed = False
            self._device = None
            self._endpoint = None
            log.debug("USB printer disconnected")

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
            PrinterError: On USB write failures.
        """
        if not data:
            return
        if self._device is None or self._endpoint is None:
            raise PrinterNotConnectedError("Printer is not connected")

        with self._lock:
            try:
                written = self._endpoint.write(data, self.timeout_ms)
            except usb.core.USBError as exc:  # type: ignore[name-defined]
                raise PrinterError(f"USB write failed: {exc}") from exc

        if written != len(data):
            raise PrinterError(
                f"Incomplete write, expected {len(data)} bytes but sent {written}"
            )

    # --------------------------------------------------------------------- #
    # Context manager helpers

    def __enter__(self) -> "USBPrinter":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    # --------------------------------------------------------------------- #
    # Internal helpers

    def _resolve_endpoint(self, interface) -> "usb.core.Endpoint":  # type: ignore[name-defined]
        """
        Locate the OUT endpoint either by explicit address or by discovery.
        """
        if self.out_endpoint is not None:
            endpoint = usb.util.find_descriptor(  # type: ignore[name-defined]
                interface,
                bEndpointAddress=self.out_endpoint,
            )
            if endpoint is None:
                raise PrinterError(
                    f"Endpoint 0x{self.out_endpoint:02x} not found on interface {self.interface}"
                )
            return endpoint

        for endpoint in interface:
            if usb.util.endpoint_direction(endpoint.bEndpointAddress) == usb.util.ENDPOINT_OUT:  # type: ignore[name-defined]
                return endpoint
        raise PrinterError(f"No OUT endpoint found on interface {self.interface}")

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

    @staticmethod
    def _ensure_usb_available() -> None:
        if usb is None:  # type: ignore[misc]
            raise PrinterError(
                "PyUSB is not installed. Install the 'pyusb' package to use USBPrinter."
            )
