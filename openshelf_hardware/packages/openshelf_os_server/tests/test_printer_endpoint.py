from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from openshelf_os.operating_mode import OperatingMode

from openshelf_os_server.core.printer_status import (
    PrinterStatus,
    PrinterStatusProbeError,
)
from openshelf_os_server.routers import printer as printer_router


def _printer_status(**overrides: bool) -> PrinterStatus:
    values = {
        "offline": False,
        "waiting_for_online_recovery": False,
        "feed_button_active": False,
        "cover_open": False,
        "paper_out": False,
        "paper_near_end": False,
        "recoverable_error": False,
        "cutter_error": False,
        "unrecoverable_error": False,
        "auto_recoverable_error": False,
    }
    values.update(overrides)
    return PrinterStatus(raw=(0x12, 0x12, 0x12, 0x12), **values)


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
async def test_print_label_writes_barcode_when_printer_is_enabled() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=True, feed_dots=18))

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
        message="success", barcode="036000291452", printed=True
    )
    print_label.assert_called_once_with("036000291452", feed_dots=18)
    close_printer.assert_called_once_with()


@pytest.mark.anyio
async def test_print_label_simulates_hardware_in_mock_mode() -> None:
    with (
        patch.object(printer_router, "get_operating_mode", return_value=OperatingMode.mock),
        patch.object(printer_router.printer, "print_label") as print_label,
    ):
        result = await printer_router.print_label(
            printer_router.PrintLabelRequest(barcode="036000291452")
        )

    assert result == printer_router.PrintLabelResponse(
        message="success", barcode="036000291452", printed=True
    )
    print_label.assert_not_called()


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


@pytest.mark.anyio
@pytest.mark.parametrize("barcode", ["  BIN-A/42  ", " "])
async def test_print_label_preserves_printable_ascii(barcode: str) -> None:
    with patch.object(printer_router, "_print_payload", return_value=True) as print_payload:
        result = await printer_router.print_label(
            printer_router.PrintLabelRequest(barcode=barcode)
        )

    assert result == printer_router.PrintLabelResponse(
        message="success",
        barcode=barcode,
        printed=True,
    )
    print_payload.assert_called_once_with(barcode)


@pytest.mark.parametrize("barcode", ["\tlabel", "label\n", "café"])
def test_print_label_rejects_control_and_non_ascii_characters(barcode: str) -> None:
    with pytest.raises(ValidationError):
        printer_router.PrintLabelRequest(barcode=barcode)


@pytest.mark.anyio
async def test_get_printer_config_returns_persisted_feed_dots() -> None:
    config = SimpleNamespace(printer=SimpleNamespace(feed_dots=24))

    with patch.object(printer_router, "get_root_config", return_value=config):
        result = await printer_router.get_printer_config()

    assert result.feed_dots == 24


@pytest.mark.anyio
async def test_update_printer_config_persists_feed_dots() -> None:
    config = SimpleNamespace(printer=SimpleNamespace(feed_dots=18))

    with (
        patch.object(printer_router, "get_root_config", return_value=config),
        patch.object(printer_router, "save_root_config") as save_root_config,
    ):
        result = await printer_router.update_printer_config(
            printer_router.PrinterFeedConfig(feed_dots=20)
        )

    assert result.feed_dots == 20
    assert config.printer.feed_dots == 20
    save_root_config.assert_called_once_with(config)


@pytest.mark.anyio
async def test_get_printer_status_reports_healthy_printer() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=True))
    status = _printer_status()

    with (
        patch.object(printer_router, "get_settings", return_value=settings),
        patch.object(printer_router, "get_operating_mode", return_value=OperatingMode.production),
        patch.object(printer_router, "probe_printer_status", return_value=status),
    ):
        result = await printer_router.get_printer_status()

    assert result.enabled is True
    assert result.connected is True
    assert result.healthy is True
    assert result.error is None
    assert result.paper_out is False


@pytest.mark.anyio
async def test_get_printer_status_reports_paper_out() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=True))

    with (
        patch.object(printer_router, "get_settings", return_value=settings),
        patch.object(printer_router, "get_operating_mode", return_value=OperatingMode.production),
        patch.object(
            printer_router,
            "probe_printer_status",
            return_value=_printer_status(offline=True, paper_out=True),
        ),
    ):
        result = await printer_router.get_printer_status()

    assert result.connected is True
    assert result.healthy is False
    assert result.paper_out is True
    assert result.error == "paper out"


@pytest.mark.anyio
async def test_get_printer_status_surfaces_probe_error() -> None:
    settings = SimpleNamespace(printer=SimpleNamespace(enabled=True))

    with (
        patch.object(printer_router, "get_settings", return_value=settings),
        patch.object(printer_router, "get_operating_mode", return_value=OperatingMode.production),
        patch.object(
            printer_router,
            "probe_printer_status",
            side_effect=PrinterStatusProbeError("device not found: /dev/usb/lp0"),
        ),
    ):
        result = await printer_router.get_printer_status()

    assert result.connected is False
    assert result.healthy is False
    assert result.error == "device not found: /dev/usb/lp0"
