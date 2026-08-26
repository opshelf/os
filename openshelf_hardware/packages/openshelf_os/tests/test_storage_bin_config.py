import pytest
from pydantic import ValidationError

from openshelf_os.config.settings import StorageBinSettings


def test_storage_bin_accepts_maximum_item_footprint() -> None:
    configured = StorageBinSettings.model_validate(
        {
            "barcode": "900000000001",
            "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
            "inner_dimensions": {"width": 180, "height": 110, "length": 260},
            "max_item_dimensions": {"width": 60, "length": 80},
            "max_items": 6,
        }
    )

    assert configured.max_item_dimensions is not None
    assert configured.max_item_dimensions.width == 60
    assert configured.max_item_dimensions.length == 80


def test_storage_bin_rejects_non_positive_maximum_item_footprint() -> None:
    with pytest.raises(ValidationError):
        StorageBinSettings.model_validate(
            {
                "barcode": "900000000001",
                "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
                "inner_dimensions": {"width": 180, "height": 110, "length": 260},
                "max_item_dimensions": {"width": 0, "length": 80},
                "max_items": 6,
            }
        )


def test_storage_bin_rejects_item_footprint_larger_than_bin() -> None:
    with pytest.raises(ValidationError, match="must fit inside"):
        StorageBinSettings.model_validate(
            {
                "barcode": "900000000001",
                "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
                "inner_dimensions": {"width": 50, "height": 110, "length": 70},
                "max_item_dimensions": {"width": 60, "length": 80},
                "max_items": 6,
            }
        )
