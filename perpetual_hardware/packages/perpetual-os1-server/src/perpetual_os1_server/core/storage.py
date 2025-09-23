"""Storage service for managing inventory items and locations."""
import os
import json
import logging
from typing import Dict, List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, text
from sqlalchemy.orm import selectinload

from ..database.models import Item, ItemLocation, StorageCell
from .sizes import SizeDatabase, ItemInformation
from .validation import StorageValidator, StorageCellValidator, LocationValidator, ValidationError
from perpetual_os1.storage import Dimensions
from perpetual_os1_server.settings import get_settings


class StorageService:
    """Service for managing storage operations with database and JSON synchronization."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def detect_migration_needed(self) -> bool:
        """Check if migration from JSON to database is needed."""
        # Use transaction with isolation level to prevent race conditions
        settings = get_settings()
        json_exists = os.path.isfile(settings.sizes.filename)

        if not json_exists:
            return False

        # Use SELECT FOR UPDATE to lock the table during check
        try:
            result = await self.session.execute(
                select(Item).with_for_update()
            )
            items_exist = result.scalar() is not None
        except Exception as e:
            # If locking fails, fallback to non-locking check
            result = await self.session.execute(
                select(Item)
            )
            items_exist = result.scalar() is not None

        return json_exists and not items_exist

    async def migrate_from_json(self) -> bool:
        """Migrate existing JSON data to database."""
        try:
            # Load the existing JSON data
            size_db = get_size_db()
            json_data = size_db.items

            if not json_data:
                # Even with no JSON data, ensure database is properly initialized
                await self.session.commit()
                return True

            # First, validate all items to ensure data integrity before migration
            validated_items = []
            for upca, item_info in json_data.items():
                # Validate item data
                if not isinstance(item_info.data, dict):
                    raise ValidationError(f"Invalid item data for UPCA {upca}")

                name = item_info.data.get("name", "Unknown")
                if not name:
                    raise ValidationError(f"Missing name for UPCA {upca}")

                # Convert weight to proper numeric format if it exists
                weight = item_info.data.get("weight")
                if weight is not None:
                    try:
                        weight = float(weight) if isinstance(weight, (int, float)) else float(str(weight))
                    except (ValueError, TypeError):
                        weight = None

                # Validate weight
                if weight is not None and weight <= 0:
                    raise ValidationError(f"Invalid weight value for UPCA {upca}")

                validated_items.append({
                    'upca': upca,
                    'name': name,
                    'dimensions': item_info.size.model_dump(),
                    'weight': weight,
                    'metadata': item_info.data.get("metadata", {}),
                    'location_data': {
                        'shelf_idx': item_info.data.get("shelf_idx"),
                        'cell_idx': item_info.data.get("cell_idx")
                    }
                })

            # Clear existing data only after validation passes
            await self.session.execute(delete(ItemLocation))
            await self.session.execute(delete(Item))
            await self.session.commit()

            # Migrate validated items
            migrated_items = {}
            for item_data in validated_items:
                item = Item(
                    upca=item_data['upca'],
                    name=item_data['name'],
                    dimensions=item_data['dimensions'],
                    weight=item_data['weight'],
                    item_metadata=item_data['metadata']
                )
                self.session.add(item)
                await self.session.flush()
                migrated_items[item.id] = item

                # Create storage location if specified in data and valid
                loc_data = item_data['location_data']
                if (loc_data['shelf_idx'] is not None and
                    loc_data['cell_idx'] is not None and
                    loc_data['shelf_idx'] >= 0 and loc_data['cell_idx'] >= 0):

                    # Validate location indices
                    LocationValidator.validate_location(loc_data['shelf_idx'], loc_data['cell_idx'])

                    location = ItemLocation(
                        item_id=item.id,
                        shelf_idx=loc_data['shelf_idx'],
                        cell_idx=loc_data['cell_idx']
                    )
                    self.session.add(location)

            await self.session.commit()
            return True

        except ValidationError:
            # Re-raise validation errors
            await self.session.rollback()
            raise
        except Exception as e:
            # Log the specific error for debugging
            logging.error(f"Migration failed: {str(e)}")
            await self.session.rollback()
            raise RuntimeError(f"Migration failed: {str(e)}") from e

    async def create_item(self, upca: str, name: str, dimensions: Dict[str, float],
                        weight: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None,
                        sync_to_json: bool = True) -> Item:
        """Create a new item in the database.

        Args:
            upca: Universal Product Code
            name: Item name
            dimensions: Item dimensions as dict
            weight: Item weight
            metadata: Additional metadata
            sync_to_json: Whether to sync to JSON after successful creation
        """
        # Validate input data
        StorageValidator.validate_item_creation(upca, name, dimensions, weight, metadata)

        try:
            item = Item(
                upca=upca,
                name=name,
                dimensions=dimensions,
                weight=weight,
                item_metadata=metadata or {}
            )
            self.session.add(item)
            await self.session.flush()

            # Commit the transaction before JSON sync
            await self.session.commit()

            # Sync to JSON after successful commit
            if sync_to_json:
                try:
                    await self.sync_to_json(transaction_committed=True)
                except Exception as json_error:
                    logging.error(f"JSON sync failed after item creation: {str(json_error)}")
                    # Don't raise - database operation was successful

            return item
        except Exception as e:
            await self.session.rollback()
            raise

    async def get_item(self, upca: str) -> Optional[Item]:
        """Get an item by UPCA."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        return result.scalar_one_or_none()

    async def get_all_items(self, skip: int = 0, limit: int = 100) -> List[Item]:
        """Get all items with pagination."""
        result = await self.session.execute(
            select(Item)
            .offset(skip)
            .limit(limit)
            .order_by(Item.created_at)
        )
        return result.scalars().all()

    async def get_items_count(self) -> int:
        """Get total count of items."""
        result = await self.session.execute(
            select(Item)
        )
        return len(result.scalars().all())

    async def update_item(self, upca: str, name: Optional[str] = None,
                        dimensions: Optional[Dict[str, float]] = None,
                        weight: Optional[float] = None,
                        metadata: Optional[Dict[str, Any]] = None) -> Optional[Item]:
        """Update an existing item."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        item = result.scalar_one_or_none()

        if not item:
            return None

        # Validate input data
        StorageValidator.validate_item_update(name, dimensions, weight, metadata)

        try:
            if name is not None:
                item.name = name
            if dimensions is not None:
                item.dimensions = dimensions
            if weight is not None:
                item.weight = weight
            if metadata is not None:
                item.item_metadata = metadata

            await self.session.commit()
            return item
        except Exception as e:
            await self.session.rollback()
            raise

    async def delete_item(self, upca: str) -> bool:
        """Delete an item and its locations."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        item = result.scalar_one_or_none()

        if not item:
            return False

        try:
            # Delete locations first (foreign key constraint)
            await self.session.execute(
                delete(ItemLocation).where(ItemLocation.item_id == item.id)
            )

            # Delete the item
            await self.session.delete(item)
            await self.session.commit()
            return True
        except Exception as e:
            await self.session.rollback()
            raise

    async def get_item_locations(self, upca: str) -> List[ItemLocation]:
        """Get all locations for a specific item."""
        result = await self.session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == upca)
            .order_by(ItemLocation.created_at)
        )
        return result.scalars().all()

    async def add_item_location(self, upca: str, shelf_idx: int, cell_idx: int) -> Optional[ItemLocation]:
        """Add a location for an item."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        item = result.scalar_one_or_none()

        if not item:
            return None

        # Validate location data
        StorageValidator.validate_location_assignment(shelf_idx, cell_idx)

        try:
            location = ItemLocation(
                item_id=item.id,
                shelf_idx=shelf_idx,
                cell_idx=cell_idx
            )
            self.session.add(location)
            await self.session.commit()
            return location
        except Exception as e:
            await self.session.rollback()
            raise

    async def remove_item_location(self, upca: str, shelf_idx: int, cell_idx: int) -> bool:
        """Remove a specific location for an item."""
        result = await self.session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == upca)
            .where(ItemLocation.shelf_idx == shelf_idx)
            .where(ItemLocation.cell_idx == cell_idx)
        )
        location = result.scalar_one_or_none()

        if not location:
            return False

        try:
            await self.session.delete(location)
            await self.session.commit()
            return True
        except Exception as e:
            await self.session.rollback()
            raise

    async def get_storage_cells(self, shelf_idx: Optional[int] = None,
                               cell_idx: Optional[int] = None) -> List[StorageCell]:
        """Get storage cells with optional filtering."""
        query = select(StorageCell)
        if shelf_idx is not None:
            query = query.where(StorageCell.shelf_idx == shelf_idx)
        if cell_idx is not None:
            query = query.where(StorageCell.cell_idx == cell_idx)

        result = await self.session.execute(query.order_by(StorageCell.shelf_idx, StorageCell.cell_idx))
        return result.scalars().all()

    async def update_storage_cell(self, shelf_idx: int, cell_idx: int, status: str) -> bool:
        """Update storage cell status."""
        result = await self.session.execute(
            update(StorageCell)
            .where(StorageCell.shelf_idx == shelf_idx)
            .where(StorageCell.cell_idx == cell_idx)
            .values(status=status)
            .returning(StorageCell)
        )
        cell = result.scalar_one_or_none()

        if cell:
            try:
                await self.session.commit()
                return True
            except Exception as e:
                await self.session.rollback()
                raise
        return False

    async def sync_to_json(self, transaction_committed: bool = False) -> bool:
        """Sync database data to JSON file for hardware compatibility.

        Args:
            transaction_committed: If True, database transaction has been committed and this is a post-sync operation
        """
        try:
            settings = get_settings()
            json_path = settings.sizes.filename

            # Get all items with locations
            result = await self.session.execute(
                select(Item)
                .options(selectinload(Item.locations))
                .order_by(Item.created_at)
            )
            items = result.scalars().all()

            # Convert to legacy format
            json_data = {}
            for item in items:
                try:
                    # Validate item data before conversion
                    if not item.upca or not item.name:
                        raise ValidationError(f"Invalid item data: missing upca or name for item ID {item.id}")

                    # Validate dimensions format
                    if not isinstance(item.dimensions, dict):
                        raise ValidationError(f"Invalid dimensions format for item {item.upca}")

                    # Validate weight if present
                    weight_value = 0
                    if item.weight is not None:
                        try:
                            weight_value = float(item.weight)
                        except (ValueError, TypeError):
                            weight_value = 0

                    # Find location if available
                    location = item.locations[0] if item.locations else None

                    # Validate location if present
                    if location:
                        if location.shelf_idx < 0 or location.cell_idx < 0:
                            raise ValidationError(f"Invalid location for item {item.upca}: negative indices")

                    item_info = ItemInformation(
                        size=Dimensions(**item.dimensions),
                        data={
                            "name": item.name,
                            "weight": weight_value,
                            "metadata": item.item_metadata or {},
                            "shelf_idx": location.shelf_idx if location else None,
                            "cell_idx": location.cell_idx if location else None
                        }
                    )
                    json_data[item.upca] = item_info

                except ValidationError:
                    raise
                except Exception as conversion_error:
                    logging.error(f"Failed to convert item {item.upca if item else 'unknown'}: {str(conversion_error)}")
                    raise ValidationError(f"Failed to convert item data: {str(conversion_error)}")

            # Ensure output directory exists - skip if filename has no directory component
            dirname = os.path.dirname(json_path)
            if dirname:  # Only create directory if there's a directory component
                os.makedirs(dirname, exist_ok=True)

            # Write to JSON file with error handling
            try:
                with open(json_path, "w+") as f:
                    json.dump(json_data, f, indent=2, default=str)
            except (IOError, OSError) as file_error:
                logging.error(f"Failed to write JSON file: {str(file_error)}")
                return False

            return True

        except ValidationError:
            raise
        except Exception as e:
            logging.error(f"JSON sync failed: {str(e)}")
            raise RuntimeError(f"JSON sync failed: {str(e)}") from e

    async def create_storage_cell(self, shelf_idx: int, cell_idx: int, capacity: str) -> StorageCell:
        """Create a new storage cell."""
        # Validate input data
        StorageCellValidator.validate_capacity(capacity)
        StorageCellValidator.validate_status("available")  # New cells default to available
        LocationValidator.validate_location(shelf_idx, cell_idx)

        # Check if storage cell already exists with proper locking
        try:
            result = await self.session.execute(
                select(StorageCell)
                .where(StorageCell.shelf_idx == shelf_idx)
                .where(StorageCell.cell_idx == cell_idx)
                .with_for_update()
            )
            existing_cell = result.scalar_one_or_none()

            if existing_cell:
                raise ValidationError(f"Storage cell at shelf {shelf_idx}, cell {cell_idx} already exists")
        except Exception as e:
            # If locking fails, try without locking as fallback
            result = await self.session.execute(
                select(StorageCell)
                .where(StorageCell.shelf_idx == shelf_idx)
                .where(StorageCell.cell_idx == cell_idx)
            )
            existing_cell = result.scalar_one_or_none()

            if existing_cell:
                raise ValidationError(f"Storage cell at shelf {shelf_idx}, cell {cell_idx} already exists")
            else:
                raise ValidationError(f"Failed to acquire lock for storage cell: {str(e)}")

        try:
            cell = StorageCell(
                shelf_idx=shelf_idx,
                cell_idx=cell_idx,
                capacity=capacity,
                status="available"
            )
            self.session.add(cell)
            await self.session.flush()
            await self.session.commit()
            return cell
        except Exception as e:
            await self.session.rollback()
            raise