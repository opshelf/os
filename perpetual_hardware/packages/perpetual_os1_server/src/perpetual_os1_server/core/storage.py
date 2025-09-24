"""Storage service for managing inventory items and locations."""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, cast
from weakref import WeakKeyDictionary

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..database.models import Item, ItemLocation, StorageCell
from .sizes import ItemInformation, _coerce_dimensions, get_size_db, get_sizes_filename
from .validation import LocationValidator, StorageCellValidator, StorageValidator, ValidationError


_session_locks: "WeakKeyDictionary[AsyncSession, asyncio.Lock]" = WeakKeyDictionary()


def _get_session_lock(session: AsyncSession) -> asyncio.Lock:
    """Return an async lock associated with the given session."""
    lock = _session_locks.get(session)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session] = lock
    return lock


class StorageService:
    """Service for managing storage operations with database and JSON synchronization."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self._lock = _get_session_lock(session)

    @staticmethod
    def _normalize_weight(weight: Optional[float]) -> Optional[float]:
        if weight is None:
            return None
        return float(weight)

    async def detect_migration_needed(self) -> bool:
        """Check if migration from JSON to database is needed."""
        filename = get_sizes_filename()
        json_exists = os.path.isfile(filename)

        if not json_exists:
            return False

        try:
            result = await self.session.execute(
                select(Item).with_for_update()
            )
            items_exist = result.scalar() is not None
        except Exception:
            result = await self.session.execute(
                select(Item)
            )
            items_exist = result.scalar() is not None

        return json_exists and not items_exist

    async def migrate_from_json(self) -> bool:
        """Migrate existing JSON data to database."""
        try:
            size_db = get_size_db()
            json_data = size_db.items

            if not json_data:
                async with self._lock:
                    await self.session.commit()
                return True

            validated_items = []
            for upca, item_info in json_data.items():
                if not isinstance(item_info.data, dict):
                    raise ValidationError(f"Invalid item data for UPCA {upca}")

                name = item_info.data.get("name", "Unknown")
                if not name:
                    raise ValidationError(f"Missing name for UPCA {upca}")

                weight = item_info.data.get("weight")
                if weight is not None:
                    try:
                        weight = float(weight) if isinstance(weight, (int, float)) else float(str(weight))
                    except (ValueError, TypeError):
                        weight = None

                if weight is not None and weight <= 0:
                    raise ValidationError(f"Invalid weight value for UPCA {upca}")

                validated_items.append({
                    'upca': upca,
                    'name': name,
                    'dimensions': item_info.size.model_dump(),
                    'weight': self._normalize_weight(weight),
                    'metadata': item_info.data.get("metadata", {}),
                    'location_data': {
                        'shelf_idx': item_info.data.get("shelf_idx"),
                        'cell_idx': item_info.data.get("cell_idx")
                    }
                })

            async with self._lock:
                await self.session.execute(delete(ItemLocation))
                await self.session.execute(delete(Item))
                await self.session.commit()

                for item_data in validated_items:
                    item = Item(
                        upca=str(item_data['upca']),
                        name=str(item_data['name']),
                        dimensions=cast(Dict[str, Any], item_data['dimensions']),
                        weight=float(item_data['weight']) if item_data['weight'] is not None else None,
                        item_metadata=cast(Optional[Dict[str, Any]], item_data['metadata'])
                    )
                    self.session.add(item)
                    await self.session.flush()

                    loc_data = cast(dict[str, Any], item_data['location_data'])
                    if (loc_data['shelf_idx'] is not None and
                        loc_data['cell_idx'] is not None and
                        isinstance(loc_data['shelf_idx'], int) and loc_data['shelf_idx'] >= 0 and
                        isinstance(loc_data['cell_idx'], int) and loc_data['cell_idx'] >= 0):

                        LocationValidator.validate_location(int(loc_data['shelf_idx']), int(loc_data['cell_idx']))

                        location = ItemLocation(
                            item_id=item.id,
                            shelf_idx=loc_data['shelf_idx'],
                            cell_idx=loc_data['cell_idx']
                        )
                        self.session.add(location)

                await self.session.commit()
            return True

        except ValidationError:
            await self.session.rollback()
            raise
        except Exception as e:
            logging.error(f"Migration failed: {str(e)}")
            await self.session.rollback()
            raise RuntimeError(f"Migration failed: {str(e)}") from e

    async def create_item(self, upca: str, name: str, dimensions: Dict[str, float],
                        weight: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None,
                        sync_to_json: bool = True) -> Item:
        """Create or upsert an item in the database."""
        StorageValidator.validate_item_creation(upca, name, dimensions, weight, metadata)

        normalized_metadata = metadata or {}
        normalized_weight = self._normalize_weight(weight)

        async with self._lock:
            try:
                result = await self.session.execute(
                    select(Item).where(Item.upca == upca)
                )
                item = result.scalar_one_or_none()

                if item is None:
                    item = Item(
                        upca=upca,
                        name=name,
                        dimensions=dimensions,
                        weight=normalized_weight,
                        item_metadata=normalized_metadata
                    )
                    self.session.add(item)
                else:
                    item.name = name
                    item.dimensions = dimensions
                    item.weight = normalized_weight
                    item.item_metadata = normalized_metadata

                await self.session.flush()
                await self.session.commit()
                await self.session.refresh(item)
            except Exception:
                await self.session.rollback()
                raise

        if sync_to_json:
            try:
                await self.sync_to_json(transaction_committed=True)
            except Exception as json_error:  # pragma: no cover - best effort logging
                logging.error(f"JSON sync failed after item creation: {str(json_error)}")

        return item

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
            .options(selectinload(Item.locations))
            .offset(skip)
            .limit(limit)
            .order_by(Item.created_at)
        )
        return list(result.scalars().all())

    async def get_items_count(self) -> int:
        """Get total count of items."""
        result = await self.session.execute(
            select(func.count()).select_from(Item)
        )
        return int(result.scalar_one())

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

        StorageValidator.validate_item_update(name, dimensions, weight, metadata)
        normalized_weight = self._normalize_weight(weight) if weight is not None else None

        async with self._lock:
            try:
                if name is not None:
                    item.name = name
                if dimensions is not None:
                    item.dimensions = dimensions
                if weight is not None:
                    item.weight = normalized_weight
                if metadata is not None:
                    item.item_metadata = metadata

                await self.session.commit()
                await self.session.refresh(item)
                return item
            except Exception:
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

        async with self._lock:
            try:
                await self.session.execute(
                    delete(ItemLocation).where(ItemLocation.item_id == item.id)
                )
                await self.session.delete(item)
                await self.session.commit()
                return True
            except Exception:
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
        return list(result.scalars().all())

    async def add_item_location(self, upca: str, shelf_idx: int, cell_idx: int) -> Optional[ItemLocation]:
        """Add a location for an item."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        item = result.scalar_one_or_none()

        if not item:
            return None

        StorageValidator.validate_location_assignment(shelf_idx, cell_idx)

        async with self._lock:
            try:
                location = ItemLocation(
                    item_id=item.id,
                    shelf_idx=shelf_idx,
                    cell_idx=cell_idx
                )
                self.session.add(location)
                await self.session.commit()
                await self.session.refresh(location)
                return location
            except Exception:
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

        async with self._lock:
            try:
                await self.session.delete(location)
                await self.session.commit()
                return True
            except Exception:
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
        return list(result.scalars().all())

    async def update_storage_cell(self, shelf_idx: int, cell_idx: int, status: str) -> bool:
        """Update storage cell status."""
        async with self._lock:
            result = await self.session.execute(
                update(StorageCell)
                .where(StorageCell.shelf_idx == shelf_idx)
                .where(StorageCell.cell_idx == cell_idx)
                .values(status=status)
                .returning(StorageCell)
            )
            cell = result.scalar_one_or_none()

            if cell is None:
                await self.session.rollback()
                return False

            try:
                await self.session.commit()
                return True
            except Exception:
                await self.session.rollback()
                raise

    async def sync_to_json(self, transaction_committed: bool = False) -> bool:
        """Sync database data to JSON file for hardware compatibility.

        Args:
            transaction_committed: If True, database transaction has been committed and this is a post-sync operation
        """
        try:
            result = await self.session.execute(
                select(Item)
                .options(selectinload(Item.locations))
                .order_by(Item.created_at)
            )
            items = result.scalars().all()

            json_data: Dict[str, ItemInformation] = {}
            for item in items:
                try:
                    if not item.upca or not item.name:
                        raise ValidationError(f"Invalid item data: missing upca or name for item ID {item.id}")

                    if not isinstance(item.dimensions, dict):
                        raise ValidationError(f"Invalid dimensions format for item {item.upca}")

                    weight_value: float = 0
                    if item.weight is not None:
                        try:
                            weight_value = float(item.weight)
                        except (ValueError, TypeError):
                            weight_value = 0

                    location = item.locations[0] if item.locations else None

                    if location:
                        if location.shelf_idx is not None and location.shelf_idx < 0 or location.cell_idx is not None and location.cell_idx < 0:
                            raise ValidationError(f"Invalid location for item {item.upca}: negative indices")

                    data_payload: Dict[str, Any] = {
                        "name": item.name,
                        "weight": weight_value,
                        "metadata": item.item_metadata or {},
                    }
                    if location:
                        data_payload["shelf_idx"] = location.shelf_idx
                        data_payload["cell_idx"] = location.cell_idx

                    item_info = ItemInformation(
                        size=_coerce_dimensions(item.dimensions),
                        data=data_payload,
                    )
                    json_data[item.upca] = item_info

                except ValidationError:
                    raise
                except Exception as conversion_error:
                    logging.error(f"Failed to convert item {item.upca if item else 'unknown'}: {str(conversion_error)}")
                    raise ValidationError(f"Failed to convert item data: {str(conversion_error)}")

            size_db = get_size_db()
            size_db.items = json_data

            try:
                size_db.save()
            except (OSError, IOError) as file_error:
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
        StorageCellValidator.validate_capacity(capacity)
        StorageCellValidator.validate_status("available")  # New cells default to available
        LocationValidator.validate_location(shelf_idx, cell_idx)

        async with self._lock:
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
            except Exception:
                try:
                    result = await self.session.execute(
                        select(StorageCell)
                        .where(StorageCell.shelf_idx == shelf_idx)
                        .where(StorageCell.cell_idx == cell_idx)
                    )
                    existing_cell = result.scalar_one_or_none()

                    if existing_cell:
                        raise ValidationError(f"Storage cell at shelf {shelf_idx}, cell {cell_idx} already exists")
                except Exception as exc:  # pragma: no cover - defensive path
                    raise ValidationError("Failed to check storage cell existence due to database locking issues") from exc

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
                await self.session.refresh(cell)
                return cell
            except Exception:
                await self.session.rollback()
                raise
