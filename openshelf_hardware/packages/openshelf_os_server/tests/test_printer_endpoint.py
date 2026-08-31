from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openshelf_os.operating_mode import OperatingMode

from openshelf_os_server.routers import printer as printer_router


@pytest.mark.anyio
async def test_print_label_skips_work_when_printer_is_disabled() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=False))

    with (
        patch.object(printer_router, "get_settings", return_value=settings),
        patch.object(printer_router.printer, "print_label") as print_label,
        patch.object(printer_router.printer, "close") as close_printer,
    ):
        result = await printer_router.print_label("036000291452")

    assert result == {
        "message": "success",
        "upca": "036000291452",
        "printed": False,
    }
    print_label.assert_not_called()
    close_printer.assert_not_called()


@pytest.mark.anyio
async def test_print_label_writes_barcode_when_printer_is_enabled() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=True))

    with (
        patch.object(printer_router, "get_settings", return_value=settings),
        patch.object(printer_router.printer, "print_label") as print_label,
        patch.object(printer_router.printer, "close") as close_printer,
    ):
        result = await printer_router.print_label("036000291452")

    assert result == {
        "message": "success",
        "upca": "036000291452",
        "printed": True,
    }
    print_label.assert_called_once_with("036000291452")
    close_printer.assert_called_once_with()


@pytest.mark.anyio
async def test_print_label_simulates_hardware_in_mock_mode() -> None:
    with (
        patch.object(printer_router, "get_operating_mode", return_value=OperatingMode.mock),
        patch.object(printer_router.printer, "print_label") as print_label,
    ):
        result = await printer_router.print_label("036000291452")

    assert result == {"message": "success", "upca": "036000291452", "printed": True}
    print_label.assert_not_called()
