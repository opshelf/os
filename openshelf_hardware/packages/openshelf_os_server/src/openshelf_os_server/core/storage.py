"""Storage service for managing inventory items and locations."""
import asyncio
from typing import Any, Dict, List, Optional
from weakref import WeakKeyDictionary

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from openshelf_os.storage import StorageModuleSide
from openshelf_os_server.core.storage_module import StorageModuleSideName, module_name_to_side

from ..database.models import Item, ItemLocation, StorageCell
from .validation import CellOccupiedError, LocationValidator, StorageCellValidator, StorageValidator, ValidationError


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

    async def detect_migration_needed(self) -> bool:
        """Legacy JSON migration removed; database is the single source of truth."""
        return False

    async def migrate_from_json(self) -> bool:
        """Legacy JSON migration removed; database is the single source of truth."""
        return True

    async def create_item(self, upca: str, name: str, dimensions: Optional[Dict[str, float]],
                        weight: Optional[float] = None, metadata: Optional[Dict[str, Any]] = None,
                        access_level_id: Optional[int] = None) -> Item:
        """Create or upsert an item in the database."""
        resolved_dimensions = dimensions or {"length": 1.0, "width": 1.0, "height": 1.0}

        StorageValidator.validate_item_creation(upca, name, resolved_dimensions, weight, metadata)

        normalized_metadata = metadata or {}
        normalized_weight = float(weight) if weight is not None else None

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
                        dimensions=resolved_dimensions,
                        weight=normalized_weight,
                        item_metadata=normalized_metadata,
                        access_level_id=access_level_id
                    )
                    self.session.add(item)
                else:
                    item.name = name
                    item.dimensions = resolved_dimensions
                    item.weight = normalized_weight
                    item.item_metadata = normalized_metadata
                    item.access_level_id = access_level_id  # type: ignore[assignment]

                await self.session.flush()
                await self.session.commit()
                await self.session.refresh(item)
            except Exception:
                await self.session.rollback()
                raise

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
                        metadata: Optional[Dict[str, Any]] = None,
                        access_level_id: Optional[int] = None) -> Optional[Item]:
        """Update an existing item."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        item = result.scalar_one_or_none()

        if not item:
            return None

        StorageValidator.validate_item_update(name, dimensions, weight, metadata)
        normalized_weight = float(weight) if weight is not None else None

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
                if access_level_id is not None:
                    item.access_level_id = access_level_id

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

    async def add_item_location(self, upca: str, side: StorageModuleSideName | str | StorageModuleSide, shelf_idx: int, cell_idx: int) -> Optional[ItemLocation]:
        """Add a location for an item."""
        result = await self.session.execute(
            select(Item).where(Item.upca == upca)
        )
        item = result.scalar_one_or_none()

        if item is None:
            return None

        StorageValidator.validate_location_assignment(shelf_idx, cell_idx)
        side_enum = module_name_to_side(side)

        async with self._lock:
            existing = await self.session.execute(
                select(ItemLocation)
                .where(ItemLocation.side == side_enum.value)
                .where(ItemLocation.shelf_idx == shelf_idx)
                .where(ItemLocation.cell_idx == cell_idx)
            )
            existing_location = existing.scalar_one_or_none()

            if existing_location is not None:
                raise CellOccupiedError(
                    f"Cell ({side_enum.value}, shelf={shelf_idx}, cell={cell_idx}) is already occupied by item_id={existing_location.item_id}"
                )

            try:
                location = ItemLocation(
                    item_id=item.id,
                    side=side_enum.value,
                    shelf_idx=shelf_idx,
                    cell_idx=cell_idx,
                )
                self.session.add(location)
                await self.session.commit()
                await self.session.refresh(location)
                return location
            except IntegrityError as exc:
                await self.session.rollback()
                raise CellOccupiedError(
                    f"Cell ({side_enum.value}, shelf={shelf_idx}, cell={cell_idx}) is already occupied"
                ) from exc
            except Exception:
                await self.session.rollback()
                raise

    async def remove_item_location(self, upca: str, side: StorageModuleSideName | str | StorageModuleSide, shelf_idx: int, cell_idx: int) -> bool:
        """Remove a specific location for an item."""
        side_enum = module_name_to_side(side)

        result = await self.session.execute(
            select(ItemLocation)
            .join(Item)
            .where(Item.upca == upca)
            .where(ItemLocation.side == side_enum.value)
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

    async def update_storage_cell(
        self,
        shelf_idx: int,
        cell_idx: int,
        status: Optional[str] = None,
        capacity: Optional[str] = None,
    ) -> bool:
        """Update storage cell status and/or capacity."""
        async with self._lock:
            values: Dict[str, Any] = {}
            if status is not None:
                values["status"] = status
            if capacity is not None:
                values["capacity"] = capacity

            if not values:
                return True

            result = await self.session.execute(
                update(StorageCell)
                .where(StorageCell.shelf_idx == shelf_idx)
                .where(StorageCell.cell_idx == cell_idx)
                .values(**values)
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

    async def get_all_occupied_cells(self) -> List[tuple]:
        """Return list of (side, shelf_idx, cell_idx, upca) for all occupied cells."""
        result = await self.session.execute(
            select(ItemLocation.side, ItemLocation.shelf_idx, ItemLocation.cell_idx, Item.upca)
            .join(Item)
        )
        return [(row.side, row.shelf_idx, row.cell_idx, row.upca) for row in result.all()]

    async def create_storage_cell(self, shelf_idx: int, cell_idx: int, capacity: str) -> StorageCell:
        """Create a new storage cell."""
        StorageCellValidator.validate_capacity(capacity)
        StorageCellValidator.validate_status("available")  # New cells default to available
        LocationValidator.validate_location(shelf_idx, cell_idx)

        async with self._lock:
            result = await self.session.execute(
                select(StorageCell)
                .where(StorageCell.shelf_idx == shelf_idx)
                .where(StorageCell.cell_idx == cell_idx)
                .with_for_update()
            )
            existing_cell = result.scalar_one_or_none()

            if existing_cell is not None:
                raise ValidationError(f"Storage cell at shelf {shelf_idx}, cell {cell_idx} already exists")

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
