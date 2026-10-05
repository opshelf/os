from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openshelf_os.operating_mode import OperatingMode

from openshelf_os_server.routers import printer as printer_router


@pytest.mark.anyio
async def test_print_label_skips_work_when_printer_is_disabled() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=False, feed_dots=18))

    with (
        patch.object(
            printer_router,
            "get_operating_mode",
            return_value=OperatingMode.production,
        ),
        patch.object(printer_router, "get_settings", return_value=settings),
        patch.object(printer_router.printer, "print_label") as print_label,
        patch.object(printer_router.printer, "close") as close_printer,
    ):
        result = await printer_router.print_label(
            printer_router.PrintLabelRequest(barcode="036000291452")
        )

    assert result == printer_router.PrintLabelResponse(
        message="success", barcode="036000291452", printed=False
    )
    print_label.assert_not_called()
    close_printer.assert_not_called()


@pytest.mark.anyio
@pytest.mark.anyio
@pytest.mark.parametrize("barcode", ["CR00006174", "CR-0000.6174/ A", "x" * 255])
async def test_print_label_simulates_bespoke_barcodes_in_mock_mode(barcode: str) -> None:
    """Mock printing accepts the arbitrary customer barcodes item CRUD accepts."""
    with (
        patch.object(printer_router, "get_operating_mode", return_value=OperatingMode.mock),
        patch.object(printer_router.printer, "print_label") as print_label,
    ):
        result = await printer_router.print_label(printer_router.PrintLabelRequest(barcode=barcode))

    assert result == printer_router.PrintLabelResponse(
        message="success", barcode=barcode, printed=True
    )
    print_label.assert_not_called()

