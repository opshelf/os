import pytest
from pydantic import ValidationError

from openshelf_os.config.settings import StorageBinSettings


@pytest.mark.parametrize(
    "inner, item, error",
    [
        pytest.param(
            {"width": 180, "height": 110, "length": 260},
            {"width": 60, "length": 80},
            None,
            id="maximum_footprint_accepted",
        ),
        pytest.param(
            {"width": 180, "height": 110, "length": 260},
            {"width": 0, "length": 80},
            "greater than 0",
            id="non_positive_rejected",
        ),
        pytest.param(
            {"width": 50, "height": 110, "length": 70},
            {"width": 60, "length": 80},
            "must fit inside",
            id="footprint_larger_than_bin",
        ),
    ],
)
def test_storage_bin_validates_item_footprint(
    inner: dict[str, float], item: dict[str, float], error: str | None
) -> None:
    payload = {
        "barcode": "900000000001",
        "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
        "inner_dimensions": inner,
        "max_item_dimensions": item,
        "max_items": 6,
    }

    if error is None:
        configured = StorageBinSettings.model_validate(payload)

        assert configured.max_item_dimensions is not None
        assert configured.max_item_dimensions.width == item["width"]
        assert configured.max_item_dimensions.length == item["length"]
        return

    with pytest.raises(ValidationError, match=error):
        StorageBinSettings.model_validate(payload)


def test_compartment_capacity_defaults_to_layout_product():
    configured = StorageBinSettings.model_validate({
        "barcode": "900000000001",
        "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
        "inner_dimensions": {"width": 200, "height": 100, "length": 160},
        "compartment_layout": {"rows": 2, "columns": 4},
    })
    assert configured.max_items == 8
    assert configured.compartment_layout.model_dump() == {"rows": 2, "columns": 4}


@pytest.mark.parametrize("rows,columns,max_items", [(0,4,4), (2,-1,2), (1.5,4,4), (True,4,4), (33,1,33), (2,4,9)])
def test_invalid_compartment_layout(rows, columns, max_items):
    with pytest.raises(ValidationError):
        StorageBinSettings.model_validate({
            "barcode": "900000000001",
            "home_location": {"side": "left_module", "shelf_idx": 2, "cell_idx": 0},
            "inner_dimensions": {"width": 200, "height": 100, "length": 160},
            "compartment_layout": {"rows": rows, "columns": columns},
            "max_items": max_items,
        })
