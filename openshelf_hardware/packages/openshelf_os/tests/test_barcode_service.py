import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

try:
    import serial_asyncio as _serial_asyncio_available  # noqa: F401
except Exception:
    sys.modules.setdefault("serial_asyncio", types.SimpleNamespace())

try:
    import serial as _serial_available  # noqa: F401
except Exception:
    serial_tools = types.ModuleType("serial.tools")
    serial_tools.list_ports = types.SimpleNamespace(comports=lambda: [])
    serial_module = types.ModuleType("serial")
    serial_module.tools = serial_tools
    sys.modules.setdefault("serial", serial_module)
    sys.modules.setdefault("serial.tools", serial_tools)
    sys.modules.setdefault("serial.tools.list_ports", serial_tools.list_ports)

from openshelf_os.barcode_service import BarcodeService
import openshelf_os.barcode_service as barcode_service


class FakeTransport:
    def __init__(self) -> None:
        self.is_closed = False

    def close(self) -> None:
        self.is_closed = True


@pytest.mark.asyncio
async def test_reconnects_after_transport_connection_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    protocols: list[Any] = []

    async def create_serial_connection(loop, protocol_factory, port, baudrate):
        protocol = protocol_factory()
        protocols.append(protocol)
        return FakeTransport(), protocol

    monkeypatch.setattr(
        barcode_service.serial_asyncio,
        "create_serial_connection",
        create_serial_connection,
        raising=False,
    )

    service = BarcodeService(port="/dev/scanner")
    await service.start()

    try:
        await asyncio.wait_for(_wait_for_protocol_count(protocols, 1), timeout=1)
        protocols[0].connection_lost(None)
        await asyncio.wait_for(_wait_for_protocol_count(protocols, 2), timeout=1)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_wait_connected_resolves_after_transport_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    protocols: list[Any] = []

    async def create_serial_connection(loop, protocol_factory, port, baudrate):
        protocol = protocol_factory()
        protocols.append(protocol)
        return FakeTransport(), protocol

    monkeypatch.setattr(
        barcode_service.serial_asyncio,
        "create_serial_connection",
        create_serial_connection,
        raising=False,
    )

    service = BarcodeService(port="/dev/scanner")
    assert await service.wait_connected(timeout=0) is False

    await service.start()

    try:
        assert await service.wait_connected(timeout=1) is True
        assert len(protocols) == 1
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_anext_propagates_cancelled_error() -> None:
    service = BarcodeService(port="/dev/scanner")
    task = asyncio.create_task(service.__anext__())

    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def _wait_for_protocol_count(protocols: list[Any], expected: int) -> None:
    while len(protocols) < expected:
        await asyncio.sleep(0)


def _write_hid_device(root: Path, name: str, hid_name: str) -> None:
    device = root / name / "device"
    device.mkdir(parents=True)
    (device / "uevent").write_text(
        "DRIVER=hid-generic\n"
        "HID_ID=0003:000028E9:00000383\n"
        f"HID_NAME={hid_name}\n"
    )


def test_find_hid_scanner_names_ignores_other_hid_devices(tmp_path: Path) -> None:
    _write_hid_device(tmp_path, "hidraw0", "USB Keyboard")
    _write_hid_device(tmp_path, "hidraw2", "USB HID Barcode Scanner")

    assert barcode_service.find_hid_scanner_names(tmp_path) == ["USB HID Barcode Scanner"]


def test_find_hid_scanner_names_when_sysfs_missing(tmp_path: Path) -> None:
    assert barcode_service.find_hid_scanner_names(tmp_path / "absent") == []


def test_describe_missing_scanner_reports_hid_mode(tmp_path: Path) -> None:
    _write_hid_device(tmp_path, "hidraw2", "USB HID Barcode Scanner")

    assert barcode_service.describe_missing_scanner(tmp_path) == barcode_service.SCANNER_HID_MODE_REASON


def test_describe_missing_scanner_reports_absent_scanner(tmp_path: Path) -> None:
    _write_hid_device(tmp_path, "hidraw0", "USB Keyboard")

    assert barcode_service.describe_missing_scanner(tmp_path) == barcode_service.SCANNER_NOT_DETECTED_REASON
