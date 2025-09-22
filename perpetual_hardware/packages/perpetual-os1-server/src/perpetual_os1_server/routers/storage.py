"""Storage management endpoints with authorization."""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Body
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict
import logging
import time
import functools

from ..database.models import Item, ItemLocation, StorageCell
from ..core.storage import StorageService
from ..core.sizes import migrate_if_needed
from ..core.validation import ValidationError
from .auth_deps import get_current_user, require_permissions
from ..database.session import get_db

router = APIRouter(tags=["storage"])

# Performance monitoring decorator
def monitor_performance(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            logging.info(f"Storage operation {func.__name__} completed in {duration:.3f}s")
            return result
        except Exception as e:
            duration = time.time() - start_time
            logging.error(f"Storage operation {func.__name__} failed after {duration:.3f}s: {str(e)}")
            raise
    return wrapper


# Pydantic models for request/response
class ItemCreate(BaseModel):
    upca: str
    name: str
    dimensions: Dict[str, float]
    weight: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    dimensions: Optional[Dict[str, float]] = None
    weight: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None


class ItemLocationCreate(BaseModel):
    shelf_idx: int
    cell_idx: int


class StorageCellUpdate(BaseModel):
    capacity: str
    status: str


class StorageCellCreate(BaseModel):
    shelf_idx: int
    cell_idx: int
    capacity: str


class ItemWithLocations(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int
    upca: str
    name: str
    dimensions: Dict[str, float]
    weight: Optional[float]
    metadata: Optional[Dict[str, Any]]
    locations: List[Any]  # Temporary fix for SQLAlchemy model
    created_at: str
    updated_at: str


class ItemLocationResponse(BaseModel):
    id: int
    item_id: int
    shelf_idx: int
    cell_idx: int
    created_at: str
    updated_at: str


class StorageCellResponse(BaseModel):
    id: int
    shelf_idx: int
    cell_idx: int
    capacity: str
    status: str
    created_at: str
    updated_at: str


class PaginatedItemsResponse(BaseModel):
    items: List[ItemWithLocations]
    total: int
    skip: int
    limit: int


# Item endpoints
@router.get("/items")
@monitor_performance
async def get_items(
    skip: int = 0,
    limit: int = 50,
    session: AsyncSession = Depends(get_db)
) -> dict:
    """Get all items with pagination."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    items = await storage_service.get_all_items(skip=skip, limit=limit)
    total = await storage_service.get_items_count()

    return {
        "items": items,
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.get("/items/{upca}", dependencies=[Depends(require_permissions("storage:read"))])
async def get_item(
    upca: str,
    session: AsyncSession = Depends(get_db)
) -> ItemWithLocations:
    """Get a specific item by UPCA."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    item = await storage_service.get_item(upca)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(upca)

    return ItemWithLocations(
        id=item.id,
        upca=item.upca,
        name=item.name,
        dimensions=item.dimensions,
        weight=float(item.weight) if item.weight else None,
        metadata=item.metadata,
        locations=locations,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat()
    )


@router.post("/items", dependencies=[Depends(require_permissions("storage:write"))])
async def create_item(
    item: ItemCreate,
    session: AsyncSession = Depends(get_db)
) -> ItemWithLocations:
    """Create a new item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    try:
        db_item = await storage_service.create_item(
            upca=item.upca,
            name=item.name,
            dimensions=item.dimensions,
            weight=item.weight,
            metadata=item.metadata or {}
        )

        locations = await storage_service.get_item_locations(item.upca)

        return ItemWithLocations(
            id=db_item.id,
            upca=db_item.upca,
            name=db_item.name,
            dimensions=db_item.dimensions,
            weight=float(db_item.weight) if db_item.weight else None,
            metadata=db_item.metadata,
            locations=locations,
            created_at=db_item.created_at.isoformat(),
            updated_at=db_item.updated_at.isoformat()
        )

    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/items/{upca}", dependencies=[Depends(require_permissions("storage:write"))])
async def update_item(
    upca: str,
    item: ItemUpdate,
    session: AsyncSession = Depends(get_db)
) -> ItemWithLocations:
    """Update an existing item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    updated_item = await storage_service.update_item(
        upca=upca,
        name=item.name,
        dimensions=item.dimensions,
        weight=item.weight,
        metadata=item.metadata
    )

    if not updated_item:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(upca)

    return ItemWithLocations(
        id=updated_item.id,
        upca=updated_item.upca,
        name=updated_item.name,
        dimensions=updated_item.dimensions,
        weight=float(updated_item.weight) if updated_item.weight else None,
        metadata=updated_item.metadata,
        locations=locations,
        created_at=updated_item.created_at.isoformat(),
        updated_at=updated_item.updated_at.isoformat()
    )


@router.delete("/items/{upca}", dependencies=[Depends(require_permissions("storage:delete"))])
@monitor_performance
async def delete_item(
    upca: str,
    session: AsyncSession = Depends(get_db)
) -> Dict[str, str]:
    """Delete an item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    success = await storage_service.delete_item(upca)
    if not success:
        raise HTTPException(status_code=404, detail="Item not found")

    try:
        await storage_service.sync_to_json(transaction_committed=True)
    except Exception as json_error:
        logging.error(f"JSON sync failed after item deletion: {str(json_error)}")
        # Don't raise - database operation was successful

    return {"message": "Item deleted successfully"}


# Item location endpoints
@router.post("/items/{upca}/locations", dependencies=[Depends(require_permissions("storage:write"))])
async def add_item_location(
    upca: str,
    location: ItemLocationCreate,
    session: AsyncSession = Depends(get_db)
) -> ItemLocationResponse:
    """Add a location for an item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    db_item = await storage_service.get_item(upca)
    if not db_item:
        raise HTTPException(status_code=404, detail="Item not found")

    storage_location = await storage_service.add_item_location(
        shelf_idx=location.shelf_idx,
        cell_idx=location.cell_idx,
        upca=upca
    )

    if not storage_location:
        raise HTTPException(status_code=400, detail="Failed to add location")

    await storage_service.sync_to_json()

    # Convert SQLAlchemy model to Pydantic response model
    return ItemLocationResponse(
        id=storage_location.id,
        item_id=storage_location.item_id,
        shelf_idx=storage_location.shelf_idx,
        cell_idx=storage_location.cell_idx,
        created_at=storage_location.created_at.isoformat() if storage_location.created_at else "",
        updated_at=storage_location.updated_at.isoformat() if storage_location.updated_at else ""
    )


@router.delete("/items/{upca}/locations", dependencies=[Depends(require_permissions("storage:write"))])
async def remove_item_location(
    upca: str,
    shelf_idx: int,
    cell_idx: int,
    session: AsyncSession = Depends(get_db)
) -> Dict[str, str]:
    """Remove a specific location for an item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    success = await storage_service.remove_item_location(
        upca=upca,
        shelf_idx=shelf_idx,
        cell_idx=cell_idx
    )

    if not success:
        raise HTTPException(status_code=404, detail="Location not found")

    await storage_service.sync_to_json()
    return {"message": "Location removed successfully"}


# Storage cell endpoints
@router.post("/locations", dependencies=[Depends(require_permissions("storage:write"))])
async def create_storage_cell(
    cell: StorageCellCreate,
    session: AsyncSession = Depends(get_db)
) -> StorageCellResponse:
    """Create a new storage cell."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    try:
        storage_cell = await storage_service.create_storage_cell(
            shelf_idx=cell.shelf_idx,
            cell_idx=cell.cell_idx,
            capacity=cell.capacity
        )

        return StorageCellResponse(
            id=storage_cell.id,
            shelf_idx=storage_cell.shelf_idx,
            cell_idx=storage_cell.cell_idx,
            capacity=storage_cell.capacity,
            status=storage_cell.status,
            created_at=storage_cell.created_at.isoformat(),
            updated_at=storage_cell.updated_at.isoformat()
        )

    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/locations", dependencies=[Depends(require_permissions("storage:read"))])
async def get_storage_cells(
    shelf_idx: Optional[int] = Query(None, description="Filter by shelf index"),
    cell_idx: Optional[int] = Query(None, description="Filter by cell index"),
    session: AsyncSession = Depends(get_db)
) -> List[StorageCellResponse]:
    """Get storage cells with optional filtering."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    cells = await storage_service.get_storage_cells(shelf_idx=shelf_idx, cell_idx=cell_idx)

    return [
        StorageCellResponse(
            id=cell.id,
            shelf_idx=cell.shelf_idx,
            cell_idx=cell.cell_idx,
            capacity=cell.capacity,
            status=cell.status,
            created_at=cell.created_at.isoformat(),
            updated_at=cell.updated_at.isoformat()
        )
        for cell in cells
    ]


@router.put("/locations/{shelf_idx}/{cell_idx}", dependencies=[Depends(require_permissions("storage:write"))])
async def update_storage_cell(
    shelf_idx: int,
    cell_idx: int,
    cell_update: StorageCellUpdate,
    session: AsyncSession = Depends(get_db)
) -> StorageCellResponse:
    """Update storage cell status."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    success = await storage_service.update_storage_cell(
        shelf_idx=shelf_idx,
        cell_idx=cell_idx,
        status=cell_update.status
    )

    if not success:
        raise HTTPException(status_code=404, detail="Storage cell not found")

    # Get updated cell
    cells = await storage_service.get_storage_cells(shelf_idx=shelf_idx, cell_idx=cell_idx)
    if not cells:
        raise HTTPException(status_code=404, detail="Storage cell not found")

    cell = cells[0]
    return StorageCellResponse(
        id=cell.id,
        shelf_idx=cell.shelf_idx,
        cell_idx=cell.cell_idx,
        capacity=cell.capacity,
        status=cell.status,
        created_at=cell.created_at.isoformat(),
        updated_at=cell.updated_at.isoformat()
    )