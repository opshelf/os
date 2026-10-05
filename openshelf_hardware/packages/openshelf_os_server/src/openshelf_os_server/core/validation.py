"""Data validation utilities for storage operations."""
from typing import Dict, Any, Optional

from openshelf_os.config.settings import BARCODE_MAX_LENGTH


class ValidationError(Exception):
    """Custom validation error."""
    pass


class CellOccupiedError(ValidationError):
    """Raised when a storage cell is already occupied by another item."""
    pass


class ItemValidator:
    """Validator for item data."""

    @staticmethod
    def validate_dimensions(dimensions: Dict[str, float]) -> None:
        """Validate item dimensions."""
        required_keys = ["width", "height", "length"]
        for key in required_keys:
            if key not in dimensions:
                raise ValidationError(f"Missing required dimension: {key}")
            if dimensions[key] <= 0:
                raise ValidationError(f"{key} must be a positive number")

    @staticmethod
    def validate_weight(weight: Optional[float]) -> None:
        """Validate item weight."""
        if weight is not None and weight <= 0:
            raise ValidationError("Weight must be a positive number")

    @staticmethod
    def validate_name(name: str) -> None:
        """Validate item name."""
        if len(name.strip()) == 0:
            raise ValidationError("Name cannot be empty")

    @staticmethod
    def validate_metadata(metadata: Optional[Dict[str, Any]]) -> None:
        """Validate item metadata."""
        return


class LocationValidator:
    """Validator for location data."""

    @staticmethod
    def validate_shelf_idx(shelf_idx: int) -> None:
        """Validate shelf index."""
        if shelf_idx < 0:
            raise ValidationError("Shelf index cannot be negative")

    @staticmethod
    def validate_cell_idx(cell_idx: int) -> None:
        """Validate cell index."""
        if cell_idx < 0:
            raise ValidationError("Cell index cannot be negative")

    @staticmethod
    def validate_location(shelf_idx: int, cell_idx: int) -> None:
        """Validate complete location."""
        LocationValidator.validate_shelf_idx(shelf_idx)
        LocationValidator.validate_cell_idx(cell_idx)



class StorageValidator:
    """Main storage validator combining all validation logic."""

    @staticmethod
    def validate_item_creation(barcode: str, name: str, dimensions: Dict[str, float],
                             weight: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Validate complete item creation data."""
        ItemValidator.validate_name(name)
        ItemValidator.validate_dimensions(dimensions)
        ItemValidator.validate_weight(weight)
        ItemValidator.validate_metadata(metadata)

        if not isinstance(barcode, str):
            raise ValidationError("Barcode must be a string")
        if len(barcode.strip()) == 0:
            raise ValidationError("Barcode cannot be empty")
        if len(barcode) > BARCODE_MAX_LENGTH:
            raise ValidationError(f"Barcode cannot exceed {BARCODE_MAX_LENGTH} characters")

    @staticmethod
    def validate_item_update(name: Optional[str] = None, dimensions: Optional[Dict[str, float]] = None,
                           weight: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Validate item update data."""
        if name is not None:
            ItemValidator.validate_name(name)
        if dimensions is not None:
            ItemValidator.validate_dimensions(dimensions)
        if weight is not None:
            ItemValidator.validate_weight(weight)
        if metadata is not None:
            ItemValidator.validate_metadata(metadata)

    @staticmethod
    def validate_location_assignment(shelf_idx: int, cell_idx: int) -> None:
        """Validate location assignment data."""
        LocationValidator.validate_location(shelf_idx, cell_idx)