from types import SimpleNamespace
from unittest.mock import patch

from openshelf_os_server.routers import printer as printer_router


def _bin(barcode: str, side: str, shelf_idx: int, cell_idx: int) -> SimpleNamespace:
    return SimpleNamespace(
        barcode=barcode,
        home_location=SimpleNamespace(side=side, shelf_idx=shelf_idx, cell_idx=cell_idx),
    )


def test_bin_label_names_the_bins_own_home_cell() -> None:
    # A wrong cell on a bin label sends the operator to the wrong storage position.
    config = SimpleNamespace(
        storage=SimpleNamespace(
            bins=[_bin("TRAY-A", "front_module", 0, 0), _bin("TRAY-B", "back_module", 3, 12)]
        )
    )
    with patch.object(printer_router, "get_root_config", return_value=config):
        assert printer_router._bin_home_label("TRAY-A") == "F:0:0"
        assert printer_router._bin_home_label("TRAY-B") == "B:3:12"
        assert printer_router._bin_home_label("ITEM-1") is None
