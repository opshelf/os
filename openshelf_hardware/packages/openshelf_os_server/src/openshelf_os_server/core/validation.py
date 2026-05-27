"""Data validation utilities for storage operations."""
from typing import Dict, Any, Optional


class ValidationError(Exception):
    """Custom validation error."""
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


class StorageCellValidator:
    """Validator for storage cell data."""

    @staticmethod
    def validate_capacity(capacity: str) -> None:
        """Validate storage capacity."""
        if not isinstance(capacity, str):
            raise ValidationError("Capacity must be a string")
        if len(capacity.strip()) == 0:
            raise ValidationError("Capacity cannot be empty")
        if len(capacity) > 50:
            raise ValidationError("Capacity cannot exceed 50 characters")

    @staticmethod
    def validate_status(status: str) -> None:
        """Validate storage cell status."""
        if not isinstance(status, str):
            raise ValidationError("Status must be a string")
        valid_statuses = ["available", "occupied", "maintenance", "disabled"]
        if status.lower() not in valid_statuses:
            raise ValidationError(f"Status must be one of: {', '.join(valid_statuses)}")


class StorageValidator:
    """Main storage validator combining all validation logic."""

    @staticmethod
    def validate_item_creation(upca: str, name: str, dimensions: Dict[str, float],
                             weight: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Validate complete item creation data."""
        ItemValidator.validate_name(name)
        ItemValidator.validate_dimensions(dimensions)
        ItemValidator.validate_weight(weight)
        ItemValidator.validate_metadata(metadata)

        if not isinstance(upca, str):
            raise ValidationError("UPCA must be a string")
        if len(upca.strip()) == 0:
            raise ValidationError("UPCA cannot be empty")
        if len(upca) > 50:
            raise ValidationError("UPCA cannot exceed 50 characters")

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