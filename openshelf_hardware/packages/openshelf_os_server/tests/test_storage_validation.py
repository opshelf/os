from openshelf_os_server.core.validation import (
    StorageValidator
)


class TestStorageValidator:
    """Test main storage validator logic."""

    def test_bespoke_barcode_creation(self) -> None:
        """Customer barcodes are arbitrary strings rather than UPC-A digits."""
        StorageValidator.validate_item_creation(
            barcode="CR-00006174/ A",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )
