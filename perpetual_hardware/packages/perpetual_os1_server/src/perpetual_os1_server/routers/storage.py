"""Storage management endpoints with authorization."""
from typing import List, Optional, Dict, Any, cast, Callable
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict
import json
import logging
import time
import functools

from ..database.models import Item, ItemLocation, User
from ..core.storage import StorageService
from ..core.sizes import migrate_if_needed
from ..core.validation import ValidationError
from .auth_deps import require_permissions, get_current_user, can_user_retrieve_item
from ..database.session import get_db

router = APIRouter(tags=["storage"])

# Performance monitoring decorator
def monitor_performance(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
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
    name: Optional[str] = None
    dimensions: Optional[Dict[str, float]] = None
    weight: Optional[float] = None
    item_metadata: Optional[Dict[str, Any]] = None
    access_level_id: Optional[int] = None


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    dimensions: Optional[Dict[str, float]] = None
    weight: Optional[float] = None
    item_metadata: Optional[Dict[str, Any]] = None
    access_level_id: Optional[int] = None


class ItemLocationCreate(BaseModel):
    shelf_idx: int
    cell_idx: int


class Dimension(BaseModel):
    length: float
    width: float
    height: float


def _dimension_to_str(dim: Dimension) -> str:
    return json.dumps({"length": dim.length, "width": dim.width, "height": dim.height})


def _str_to_dimension(s: str) -> Dimension:
    try:
        data = json.loads(s)
        return Dimension(length=data["length"], width=data["width"], height=data["height"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return Dimension(length=0.0, width=0.0, height=0.0)


class StorageCellUpdate(BaseModel):
    capacity: Optional[Dimension] = None
    status: Optional[str] = None


class StorageCellCreate(BaseModel):
    shelf_idx: int
    cell_idx: int
    capacity: Dimension


class ItemWithLocations(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: int
    upca: str
    name: str
    dimensions: Dict[str, float]
    weight: Optional[float]
    item_metadata: Optional[Dict[str, Any]]
    access_level_id: Optional[int]
    locations: List[Any]  # Temporary fix for SQLAlchemy model
    created_at: str
    updated_at: str
    can_retrieve: bool


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
    capacity: Dimension
    status: str
    created_at: str
    updated_at: str


class PaginatedItemsResponse(BaseModel):
    items: List[ItemWithLocations]
    total: int
    skip: int
    limit: int


def _serialize_locations(locations: List[ItemLocation]) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for location in locations:
        payload.append({
            "id": location.id,
            "item_id": location.item_id,
            "shelf_idx": location.shelf_idx,
            "cell_idx": location.cell_idx,
            "created_at": location.created_at.isoformat() if location.created_at else "",
            "updated_at": location.updated_at.isoformat() if location.updated_at else "",
        })
    return payload


def _build_item_payload(item: Item, locations: List[ItemLocation], current_user: User) -> ItemWithLocations:
    return ItemWithLocations(
        id=int(item.id),
        upca=str(item.upca),
        name=str(item.name),
        dimensions=cast(dict[str, float], item.dimensions),
        weight=float(item.weight) if item.weight else None,
        item_metadata=cast(dict[str, Any], item.item_metadata) or {},
        access_level_id=item.access_level_id,
        locations=_serialize_locations(locations),
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
        can_retrieve=can_user_retrieve_item(current_user, item),
    )


# Item endpoints
@router.get("/items", dependencies=[Depends(require_permissions("storage:read"))])
@monitor_performance
async def get_items(
    response: Response,
    skip: int = 0,
    limit: int = 50,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> PaginatedItemsResponse:
    """Get all items with pagination."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    items = await storage_service.get_all_items(skip=skip, limit=limit)
    total = await storage_service.get_items_count()

    if response is not None:
        response.headers["X-Total-Count"] = str(total)

    return PaginatedItemsResponse(
        items=[_build_item_payload(item, list(item.locations), current_user) for item in items],
        total=total,
        skip=skip,
        limit=limit
    )


@router.get("/items/{upca}", dependencies=[Depends(require_permissions("storage:read"))])
async def get_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Get a specific item by UPCA."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    item = await storage_service.get_item(upca)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(upca)

    return _build_item_payload(item, locations, current_user)


@router.post("/items", dependencies=[Depends(require_permissions("storage:write"))])
async def create_item(
    item: ItemCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Create a new item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    try:
        resolved_name = item.name or item.item_metadata.get("name") if item.item_metadata else None
        resolved_dimensions = item.dimensions or {"length": 1.0, "width": 1.0, "height": 1.0}

        db_item = await storage_service.create_item(
            upca=item.upca,
            name=resolved_name or item.upca,
            dimensions=resolved_dimensions,
            weight=item.weight,
            metadata=item.item_metadata or {},
            access_level_id=item.access_level_id
        )

        locations = await storage_service.get_item_locations(item.upca)

        return _build_item_payload(db_item, locations, current_user)

    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/items/{upca}", dependencies=[Depends(require_permissions("storage:write"))])
async def update_item(
    upca: str,
    item: ItemUpdate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Update an existing item."""
    await migrate_if_needed(session)
    storage_service = StorageService(session)

    updated_item = await storage_service.update_item(
        upca=upca,
        name=item.name,
        dimensions=item.dimensions,
        weight=item.weight,
        metadata=item.item_metadata,
        access_level_id=item.access_level_id
    )

    if not updated_item:
        raise HTTPException(status_code=404, detail="Item not found")

    fresh_item = await storage_service.get_item(upca)
    if fresh_item is None:
        fresh_item = updated_item

    locations = await storage_service.get_item_locations(upca)

    return _build_item_payload(fresh_item, locations, current_user)


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
        id=int(storage_location.id),
        item_id=int(storage_location.item_id),
        shelf_idx=int(storage_location.shelf_idx),
        cell_idx=int(storage_location.cell_idx),
        created_at=storage_location.created_at.isoformat() if storage_location.created_at else "",
        updated_at=storage_location.updated_at.isoformat() if storage_location.updated_at else ""
    )


@router.delete("/items/{upca}/locations/{shelf_idx}/{cell_idx}", dependencies=[Depends(require_permissions("storage:write"))])
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
            capacity=_dimension_to_str(cell.capacity)
        )

        return StorageCellResponse(
            id=int(storage_cell.id),
            shelf_idx=int(storage_cell.shelf_idx),
            cell_idx=int(storage_cell.cell_idx),
            capacity=_str_to_dimension(storage_cell.capacity),
            status=str(storage_cell.status),
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
            id=int(cell.id),
            shelf_idx=int(cell.shelf_idx),
            cell_idx=int(cell.cell_idx),
            capacity=_str_to_dimension(cell.capacity),
            status=str(cell.status),
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
        id=int(cell.id),
        shelf_idx=int(cell.shelf_idx),
        cell_idx=int(cell.cell_idx),
        capacity=_str_to_dimension(cell.capacity),
        status=str(cell.status),
        created_at=cell.created_at.isoformat(),
        updated_at=cell.updated_at.isoformat()
    )
