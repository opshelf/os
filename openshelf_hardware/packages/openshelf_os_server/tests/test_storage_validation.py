import pytest
from openshelf_os_server.core.validation import (
    ValidationError,
    ItemValidator,
    LocationValidator,
    StorageValidator
)


class TestItemValidator:
    """Test item validation logic."""

    def test_missing_dimensions(self) -> None:
        """Test missing required dimensions."""
        with pytest.raises(ValidationError, match="Missing required dimension"):
            ItemValidator.validate_dimensions({"width": 10.0, "height": 20.0})


    def test_negative_dimensions(self) -> None:
        """Test negative dimension values."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_dimensions({"width": -10.0, "height": 20.0, "length": 30.0})

    def test_zero_dimensions(self) -> None:
        """Test zero dimension values."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_dimensions({"width": 0.0, "height": 20.0, "length": 30.0})

    def test_large_dimensions(self) -> None:
        """Test dimensions without arbitrary maximum size."""
        ItemValidator.validate_dimensions({"width": 2000.0, "height": 20.0, "length": 30.0})

    def test_invalid_weight(self) -> None:
        """Test invalid weight."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_weight(-10.0)

    def test_zero_weight(self) -> None:
        """Test zero weight."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_weight(0.0)

    def test_large_weight(self) -> None:
        """Test weight without arbitrary maximum size."""
        ItemValidator.validate_weight(2000000.0)

    def test_empty_name(self) -> None:
        """Test empty name."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            ItemValidator.validate_name("")

    def test_whitespace_name(self) -> None:
        """Test whitespace-only name."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            ItemValidator.validate_name("   ")

    def test_long_name(self) -> None:
        """Test name length is handled by request schemas."""
        ItemValidator.validate_name("a" * 256)

    def test_large_metadata(self) -> None:
        """Test metadata without pre-storage size checks."""
        ItemValidator.validate_metadata({"data": "x" * 11000})


class TestLocationValidator:
    """Test location validation logic."""

    def test_negative_shelf_idx(self) -> None:
        """Test negative shelf index."""
        with pytest.raises(ValidationError, match="Shelf index cannot be negative"):
            LocationValidator.validate_shelf_idx(-1)

    def test_large_shelf_idx(self) -> None:
        """Test shelf index without arbitrary maximum."""
        LocationValidator.validate_shelf_idx(1000)

    def test_negative_cell_idx(self) -> None:
        """Test negative cell index."""
        with pytest.raises(ValidationError, match="Cell index cannot be negative"):
            LocationValidator.validate_cell_idx(-1)

    def test_large_cell_idx(self) -> None:
        """Test cell index without arbitrary maximum."""
        LocationValidator.validate_cell_idx(1000)


class TestStorageValidator:
    """Test main storage validator logic."""

    def test_invalid_barcode_creation(self) -> None:
        """Test invalid barcode in item creation."""
        with pytest.raises(ValidationError, match="Barcode must be a string"):
            StorageValidator.validate_item_creation(
                barcode=123456789012,  # type: ignore[arg-type]
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_empty_barcode_creation(self) -> None:
        """Test empty barcode in item creation."""
        with pytest.raises(ValidationError, match="Barcode cannot be empty"):
            StorageValidator.validate_item_creation(
                barcode="",
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_bespoke_barcode_creation(self) -> None:
        """Customer barcodes are arbitrary strings rather than UPC-A digits."""
        StorageValidator.validate_item_creation(
            barcode="CR-00006174/ A",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
        )

    def test_long_barcode_creation(self) -> None:
        """Test barcode that exceeds the VARCHAR(255) column width."""
        long_barcode = "a" * 256
        with pytest.raises(ValidationError, match="Barcode cannot exceed 255 characters"):
            StorageValidator.validate_item_creation(
                barcode=long_barcode,
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_invalid_item_dimensions_creation(self) -> None:
        """Test invalid dimensions in item creation."""
        with pytest.raises(ValidationError, match="Missing required dimension"):
            StorageValidator.validate_item_creation(
                barcode="123456789012",
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0}  # missing length
            )

    def test_invalid_item_weight_creation(self) -> None:
        """Test invalid weight in item creation."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            StorageValidator.validate_item_creation(
                barcode="123456789012",
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0},
                weight=-100.0
            )

    def test_invalid_item_name_creation(self) -> None:
        """Test invalid name in item creation."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            StorageValidator.validate_item_creation(
                barcode="123456789012",
                name="",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_invalid_item_update(self) -> None:
        """Test invalid item update data."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            StorageValidator.validate_item_update(name="")

        with pytest.raises(ValidationError, match="Missing required dimension"):
            StorageValidator.validate_item_update(
                dimensions={"width": 10.0, "height": 20.0}
            )

    def test_invalid_location_assignment(self) -> None:
        """Test invalid location assignment."""

        with pytest.raises(ValidationError, match="Shelf index cannot be negative"):
            StorageValidator.validate_location_assignment(-1, 1)

        StorageValidator.validate_location_assignment(1, 1000)