import pytest
from perpetual_os1_server.core.validation import (
    ValidationError,
    ItemValidator,
    LocationValidator,
    StorageCellValidator,
    StorageValidator
)


class TestItemValidator:
    """Test item validation logic."""

    def test_valid_dimensions(self):
        """Test valid dimensions."""
        dimensions = {"width": 10.0, "height": 20.0, "length": 30.0}
        # Should not raise exception
        ItemValidator.validate_dimensions(dimensions)

    def test_missing_dimensions(self):
        """Test missing required dimensions."""
        with pytest.raises(ValidationError, match="Missing required dimension"):
            ItemValidator.validate_dimensions({"width": 10.0, "height": 20.0})

    def test_invalid_dimensions_type(self):
        """Test invalid dimensions type."""
        with pytest.raises(ValidationError, match="Dimensions must be a dictionary"):
            ItemValidator.validate_dimensions("not_a_dict")

    def test_negative_dimensions(self):
        """Test negative dimension values."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_dimensions({"width": -10.0, "height": 20.0, "length": 30.0})

    def test_zero_dimensions(self):
        """Test zero dimension values."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_dimensions({"width": 0.0, "height": 20.0, "length": 30.0})

    def test_too_large_dimensions(self):
        """Test dimensions that exceed maximum size."""
        large_dimensions = {"width": 2000.0, "height": 20.0, "length": 30.0}
        with pytest.raises(ValidationError, match="exceeds maximum allowed size"):
            ItemValidator.validate_dimensions(large_dimensions)

    def test_valid_weight(self):
        """Test valid weight."""
        # Should not raise exception
        ItemValidator.validate_weight(100.0)
        ItemValidator.validate_weight(None)

    def test_invalid_weight(self):
        """Test invalid weight."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_weight(-10.0)

    def test_zero_weight(self):
        """Test zero weight."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            ItemValidator.validate_weight(0.0)

    def test_too_large_weight(self):
        """Test weight that exceeds maximum."""
        with pytest.raises(ValidationError, match="exceeds maximum allowed weight"):
            ItemValidator.validate_weight(2000000.0)

    def test_valid_name(self):
        """Test valid name."""
        # Should not raise exception
        ItemValidator.validate_name("Valid Name")
        ItemValidator.validate_name("A")

    def test_invalid_name_type(self):
        """Test invalid name type."""
        with pytest.raises(ValidationError, match="Name must be a string"):
            ItemValidator.validate_name(123)

    def test_empty_name(self):
        """Test empty name."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            ItemValidator.validate_name("")

    def test_whitespace_name(self):
        """Test whitespace-only name."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            ItemValidator.validate_name("   ")

    def test_long_name(self):
        """Test name that exceeds maximum length."""
        long_name = "a" * 256
        with pytest.raises(ValidationError, match="Name cannot exceed 255 characters"):
            ItemValidator.validate_name(long_name)

    def test_valid_metadata(self):
        """Test valid metadata."""
        # Should not raise exception
        ItemValidator.validate_metadata(None)
        ItemValidator.validate_metadata({})
        ItemValidator.validate_metadata({"key": "value"})

    def test_invalid_metadata_type(self):
        """Test invalid metadata type."""
        with pytest.raises(ValidationError, match="Metadata must be a dictionary"):
            ItemValidator.validate_metadata("not_a_dict")

    def test_large_metadata(self):
        """Test metadata that exceeds size limit."""
        # Create large metadata string
        large_metadata = {"data": "x" * 11000}
        with pytest.raises(ValidationError, match="Metadata size exceeds maximum allowed size"):
            ItemValidator.validate_metadata(large_metadata)


class TestLocationValidator:
    """Test location validation logic."""

    def test_valid_shelf_idx(self):
        """Test valid shelf index."""
        # Should not raise exception
        LocationValidator.validate_shelf_idx(0)
        LocationValidator.validate_shelf_idx(1)
        LocationValidator.validate_shelf_idx(999)

    def test_invalid_shelf_idx_type(self):
        """Test invalid shelf index type."""
        with pytest.raises(ValidationError, match="Shelf index must be an integer"):
            LocationValidator.validate_shelf_idx("1")

    def test_negative_shelf_idx(self):
        """Test negative shelf index."""
        with pytest.raises(ValidationError, match="Shelf index cannot be negative"):
            LocationValidator.validate_shelf_idx(-1)

    def test_large_shelf_idx(self):
        """Test shelf index that exceeds maximum."""
        with pytest.raises(ValidationError, match="Shelf index exceeds maximum allowed value"):
            LocationValidator.validate_shelf_idx(1000)

    def test_valid_cell_idx(self):
        """Test valid cell index."""
        # Should not raise exception
        LocationValidator.validate_cell_idx(0)
        LocationValidator.validate_cell_idx(1)
        LocationValidator.validate_cell_idx(999)

    def test_invalid_cell_idx_type(self):
        """Test invalid cell index type."""
        with pytest.raises(ValidationError, match="Cell index must be an integer"):
            LocationValidator.validate_cell_idx("1")

    def test_negative_cell_idx(self):
        """Test negative cell index."""
        with pytest.raises(ValidationError, match="Cell index cannot be negative"):
            LocationValidator.validate_cell_idx(-1)

    def test_large_cell_idx(self):
        """Test cell index that exceeds maximum."""
        with pytest.raises(ValidationError, match="Cell index exceeds maximum allowed value"):
            LocationValidator.validate_cell_idx(1000)

    def test_valid_location(self):
        """Test valid location."""
        # Should not raise exception
        LocationValidator.validate_location(1, 1)
        LocationValidator.validate_location(0, 0)
        LocationValidator.validate_location(999, 999)

    def test_invalid_location_shelf(self):
        """Test location with invalid shelf index."""
        with pytest.raises(ValidationError, match="Shelf index must be an integer"):
            LocationValidator.validate_location("1", 1)

    def test_invalid_location_cell(self):
        """Test location with invalid cell index."""
        with pytest.raises(ValidationError, match="Cell index must be an integer"):
            LocationValidator.validate_location(1, "1")


class TestStorageCellValidator:
    """Test storage cell validation logic."""

    def test_valid_capacity(self):
        """Test valid capacity."""
        # Should not raise exception
        StorageCellValidator.validate_capacity("small")
        StorageCellValidator.validate_capacity("large")
        StorageCellValidator.validate_capacity("A")

    def test_invalid_capacity_type(self):
        """Test invalid capacity type."""
        with pytest.raises(ValidationError, match="Capacity must be a string"):
            StorageCellValidator.validate_capacity(123)

    def test_empty_capacity(self):
        """Test empty capacity."""
        with pytest.raises(ValidationError, match="Capacity cannot be empty"):
            StorageCellValidator.validate_capacity("")

    def test_whitespace_capacity(self):
        """Test whitespace-only capacity."""
        with pytest.raises(ValidationError, match="Capacity cannot be empty"):
            StorageCellValidator.validate_capacity("   ")

    def test_long_capacity(self):
        """Test capacity that exceeds maximum length."""
        long_capacity = "a" * 51
        with pytest.raises(ValidationError, match="Capacity cannot exceed 50 characters"):
            StorageCellValidator.validate_capacity(long_capacity)

    def test_valid_status(self):
        """Test valid status."""
        # Should not raise exception
        valid_statuses = ["available", "occupied", "maintenance", "disabled"]
        for status in valid_statuses:
            StorageCellValidator.validate_status(status)

    def test_invalid_status(self):
        """Test invalid status."""
        with pytest.raises(ValidationError, match="Status must be one of"):
            StorageCellValidator.validate_status("invalid_status")

    def test_invalid_status_type(self):
        """Test invalid status type."""
        with pytest.raises(ValidationError, match="Status must be a string"):
            StorageCellValidator.validate_status(123)

    def test_case_insensitive_status(self):
        """Test that status validation is case insensitive."""
        # Should not raise exception for any case
        for status in ["AVAILABLE", "Occupied", "MAINTENANCE", "disabled"]:
            StorageCellValidator.validate_status(status)


class TestStorageValidator:
    """Test main storage validator logic."""

    def test_valid_item_creation(self):
        """Test valid item creation data."""
        # Should not raise exception
        StorageValidator.validate_item_creation(
            upca="123456789012",
            name="Test Item",
            dimensions={"width": 10.0, "height": 20.0, "length": 30.0},
            weight=500.0,
            metadata={"category": "test"}
        )

    def test_invalid_upca_creation(self):
        """Test invalid UPCA in item creation."""
        with pytest.raises(ValidationError, match="UPCA must be a string"):
            StorageValidator.validate_item_creation(
                upca=123456789012,
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_empty_upca_creation(self):
        """Test empty UPCA in item creation."""
        with pytest.raises(ValidationError, match="UPCA cannot be empty"):
            StorageValidator.validate_item_creation(
                upca="",
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_long_upca_creation(self):
        """Test UPCA that exceeds maximum length."""
        long_upca = "a" * 51
        with pytest.raises(ValidationError, match="UPCA cannot exceed 50 characters"):
            StorageValidator.validate_item_creation(
                upca=long_upca,
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_invalid_item_dimensions_creation(self):
        """Test invalid dimensions in item creation."""
        with pytest.raises(ValidationError, match="Missing required dimension"):
            StorageValidator.validate_item_creation(
                upca="123456789012",
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0}  # missing length
            )

    def test_invalid_item_weight_creation(self):
        """Test invalid weight in item creation."""
        with pytest.raises(ValidationError, match="must be a positive number"):
            StorageValidator.validate_item_creation(
                upca="123456789012",
                name="Test Item",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0},
                weight=-100.0
            )

    def test_invalid_item_name_creation(self):
        """Test invalid name in item creation."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            StorageValidator.validate_item_creation(
                upca="123456789012",
                name="",
                dimensions={"width": 10.0, "height": 20.0, "length": 30.0}
            )

    def test_valid_item_update(self):
        """Test valid item update data."""
        # Should not raise exception
        StorageValidator.validate_item_update()
        StorageValidator.validate_item_update(name="New Name")
        StorageValidator.validate_item_update(
            dimensions={"width": 15.0, "height": 25.0, "length": 35.0}
        )
        StorageValidator.validate_item_update(weight=750.0)
        StorageValidator.validate_item_update(metadata={"category": "updated"})

    def test_invalid_item_update(self):
        """Test invalid item update data."""
        with pytest.raises(ValidationError, match="Name cannot be empty"):
            StorageValidator.validate_item_update(name="")

        with pytest.raises(ValidationError, match="Missing required dimension"):
            StorageValidator.validate_item_update(
                dimensions={"width": 10.0, "height": 20.0}
            )

    def test_valid_location_assignment(self):
        """Test valid location assignment."""
        # Should not raise exception
        StorageValidator.validate_location_assignment(1, 1)
        StorageValidator.validate_location_assignment(0, 0)
        StorageValidator.validate_location_assignment(999, 999)

    def test_invalid_location_assignment(self):
        """Test invalid location assignment."""
        with pytest.raises(ValidationError, match="Shelf index must be an integer"):
            StorageValidator.validate_location_assignment("1", 1)

        with pytest.raises(ValidationError, match="Cell index must be an integer"):
            StorageValidator.validate_location_assignment(1, "1")

        with pytest.raises(ValidationError, match="Shelf index cannot be negative"):
            StorageValidator.validate_location_assignment(-1, 1)

        with pytest.raises(ValidationError, match="Cell index exceeds maximum allowed value"):
            StorageValidator.validate_location_assignment(1, 1000)