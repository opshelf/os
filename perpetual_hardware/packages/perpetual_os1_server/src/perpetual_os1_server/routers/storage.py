"""Storage management endpoints with authorization."""
from __future__ import annotations

import functools
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Union, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..database.models import Item, ItemLocation, User
from ..core.storage import StorageService
from ..core.validation import ValidationError
from .auth_deps import can_user_retrieve_item, get_current_user, require_permissions
from ..database.session import get_db

router = APIRouter(tags=["storage"])


# ---------------------------------------------------------------------------
# Helper models
# ---------------------------------------------------------------------------

class DimensionsPayload(BaseModel):
    """Shared dimension representation."""

    width: float
    height: float
    length: float


class ItemCreate(BaseModel):
    """Request payload for creating an item."""

    model_config = ConfigDict(populate_by_name=True)

    upca: str
    name: str
    dimensions: Optional[Dict[str, float]] = None
    weight: Optional[float] = None
    item_metadata: Optional[Dict[str, Any]] = Field(default=None, alias="item_metadata")
    access_level_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict) and "metadata" in data and "item_metadata" not in data:
            data["item_metadata"] = data.pop("metadata")
        return data

    @model_validator(mode="after")
    def ensure_metadata(self) -> "ItemCreate":
        if self.item_metadata is None:
            self.item_metadata = {}
        return self


class ItemUpdate(BaseModel):
    """Request payload for updating an item."""

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = None
    dimensions: Optional[Dict[str, float]] = None
    weight: Optional[float] = None
    item_metadata: Optional[Dict[str, Any]] = Field(default=None, alias="item_metadata")
    access_level_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict) and "metadata" in data and "item_metadata" not in data:
            data["item_metadata"] = data.pop("metadata")
        return data


class ItemLocationCreate(BaseModel):
    """Request payload for assigning a location to an item."""

    side: str
    shelf_idx: int
    cell_idx: int


class StorageCellUpdate(BaseModel):
    """Request payload for updating a storage cell."""

    capacity: Optional[Union[str, DimensionsPayload]] = None
    status: Optional[str] = None


class CellIdentifier(BaseModel):
    side: str
    shelf_idx: int
    cell_idx: int


class BatchStatusUpdate(BaseModel):
    cells: List[CellIdentifier]
    status: str


class ItemWithLocations(BaseModel):
    """Response payload describing an item with its locations."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    id: int
    upca: str
    name: str
    dimensions: Dict[str, float]
    weight: Optional[float]
    metadata: Dict[str, Any] = Field(default_factory=dict)
    access_level_id: Optional[int]
    locations: List[Dict[str, Any]]
    created_at: str
    updated_at: str
    can_retrieve: bool


class StorageCellResponse(BaseModel):
    """Response payload for a storage cell."""

    id: int
    shelf_idx: int
    cell_idx: int
    capacity: DimensionsPayload
    status: str
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _serialize_locations(locations: List[ItemLocation]) -> List[Dict[str, Any]]:
    payload: List[Dict[str, Any]] = []
    for location in locations:
        payload.append(
            {
                "id": location.id,
                "item_id": location.item_id,
                "side": location.side,
                "shelf_idx": location.shelf_idx,
                "cell_idx": location.cell_idx,
                "created_at": location.created_at.isoformat() if location.created_at else "",
                "updated_at": location.updated_at.isoformat() if location.updated_at else "",
            }
        )
    return payload


def _build_item_payload(item: Item, locations: List[ItemLocation], current_user: User) -> ItemWithLocations:
    return ItemWithLocations(
        id=int(item.id),
        upca=str(item.upca),
        name=str(item.name),
        dimensions=cast(Dict[str, float], item.dimensions),
        weight=float(item.weight) if item.weight is not None else None,
        metadata=cast(Dict[str, Any], item.item_metadata) or {},
        access_level_id=item.access_level_id,
        locations=_serialize_locations(locations),
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
        can_retrieve=can_user_retrieve_item(current_user, item),
    )


def _serialize_capacity(capacity: Union[str, DimensionsPayload, None]) -> Optional[str]:
    if capacity is None:
        return None
    if isinstance(capacity, str):
        return capacity
    return f"{capacity.width}x{capacity.height}x{capacity.length}"


def _deserialize_capacity(capacity: str) -> DimensionsPayload:
    import json as json_module

    try:
        data = json_module.loads(capacity)
        if isinstance(data, dict):
            return DimensionsPayload(
                width=float(data.get("width", 0.0)),
                height=float(data.get("height", 0.0)),
                length=float(data.get("length", 0.0)),
            )
    except (json_module.JSONDecodeError, TypeError, ValueError):
        pass

    numbers = [float(match) for match in re.findall(r"\d+(?:\.\d+)?", capacity)]
    if len(numbers) >= 3:
        width, height, length = numbers[:3]
    else:
        width = height = length = 0.0
    return DimensionsPayload(width=width, height=height, length=length)


async def _load_item_with_locations(
    upca: str,
    storage_service: StorageService,
    current_user: User,
) -> ItemWithLocations:
    item = await storage_service.get_item(upca)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(upca)
    return _build_item_payload(item, locations, current_user)


# ---------------------------------------------------------------------------
# Performance monitoring decorator
# ---------------------------------------------------------------------------

def monitor_performance(func: Callable) -> Callable:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        try:
            result = await func(*args, **kwargs)
            duration = time.time() - start_time
            logging.info(f"Storage operation {func.__name__} completed in {duration:.3f}s")
            return result
        except Exception as exc:  # pragma: no cover - defensive logging
            duration = time.time() - start_time
            logging.error(
                "Storage operation %s failed after %.3fs: %s",
                func.__name__,
                duration,
                str(exc),
            )
            raise

    return wrapper


# ---------------------------------------------------------------------------
# Item endpoints
# ---------------------------------------------------------------------------


@router.get("/items", dependencies=[Depends(require_permissions("storage:read"))])
@monitor_performance
async def get_items(
    response: Response,
    skip: int = 0,
    limit: int = 50,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Get all items with pagination."""
    storage_service = StorageService(session)

    items = await storage_service.get_all_items(skip=skip, limit=limit)
    total = await storage_service.get_items_count()

    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Limit"] = str(limit)
        response.headers["X-Skip"] = str(skip)

    item_payloads = [
        _build_item_payload(item, list(item.locations), current_user)
        for item in items
    ]

    return {
        "items": item_payloads,
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/items/{upca}", dependencies=[Depends(require_permissions("storage:read"))])
async def get_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Get a specific item by UPCA."""
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
    storage_service = StorageService(session)

    try:
        db_item = await storage_service.create_item(
            upca=item.upca,
            name=item.name,
            dimensions=item.dimensions,
            weight=item.weight,
            metadata=item.item_metadata or {},
            access_level_id=item.access_level_id,
        )

        locations = await storage_service.get_item_locations(item.upca)

        return _build_item_payload(db_item, locations, current_user)

    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/items/{upca}", dependencies=[Depends(require_permissions("storage:write"))])
async def update_item(
    upca: str,
    item: ItemUpdate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Update an existing item."""
    storage_service = StorageService(session)

    updated_item = await storage_service.update_item(
        upca=upca,
        name=item.name,
        dimensions=item.dimensions,
        weight=item.weight,
        metadata=item.item_metadata,
        access_level_id=item.access_level_id,
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
    session: AsyncSession = Depends(get_db),
) -> Dict[str, str]:
    """Delete an item."""
    storage_service = StorageService(session)

    deleted = await storage_service.delete_item(upca)

    if deleted:
        try:
            await storage_service.sync_to_json(transaction_committed=True)
        except Exception as json_error:  # pragma: no cover - best effort logging
            logging.error(f"JSON sync failed after item deletion: {str(json_error)}")

    return {"message": "Item deleted successfully"}


# ---------------------------------------------------------------------------
# Item location endpoints
# ---------------------------------------------------------------------------


@router.post("/items/{upca}/locations", dependencies=[Depends(require_permissions("storage:write"))])
async def add_item_location(
    upca: str,
    location: ItemLocationCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Add a location for an item."""
    storage_service = StorageService(session)

    db_item = await storage_service.get_item(upca)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    storage_location = await storage_service.add_item_location(
        upca=upca,
        side=location.side,
        shelf_idx=location.shelf_idx,
        cell_idx=location.cell_idx,
    )

    if not storage_location:
        raise HTTPException(status_code=400, detail="Failed to add location")

    await storage_service.sync_to_json()

    return await _load_item_with_locations(upca, storage_service, current_user)


async def _remove_item_location_impl(
    upca: str,
    side: str,
    shelf_idx: int,
    cell_idx: int,
    storage_service: StorageService,
) -> None:
    success = await storage_service.remove_item_location(
        upca=upca,
        side=side,
        shelf_idx=shelf_idx,
        cell_idx=cell_idx,
    )

    if not success:
        raise HTTPException(status_code=404, detail="Location not found")

    await storage_service.sync_to_json()


@router.delete("/items/{upca}/locations", dependencies=[Depends(require_permissions("storage:write"))])
async def remove_item_location(
    upca: str,
    side: str,
    shelf_idx: int,
    cell_idx: int,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Remove a specific location for an item."""
    storage_service = StorageService(session)

    await _remove_item_location_impl(upca, side, shelf_idx, cell_idx, storage_service)
    return await _load_item_with_locations(upca, storage_service, current_user)


@router.delete(
    "/items/{upca}/locations/{side}/{shelf_idx}/{cell_idx}",
    dependencies=[Depends(require_permissions("storage:write"))],
)
async def remove_item_location_by_path(
    upca: str,
    side: str,
    shelf_idx: int,
    cell_idx: int,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Remove a specific location using path parameters."""
    storage_service = StorageService(session)

    await _remove_item_location_impl(upca, side, shelf_idx, cell_idx, storage_service)
    return await _load_item_with_locations(upca, storage_service, current_user)


# ---------------------------------------------------------------------------
# Storage cell endpoints
# ---------------------------------------------------------------------------


@router.get("/locations", dependencies=[Depends(require_permissions("storage:read"))])
async def get_storage_cells(
    shelf_idx: Optional[int] = Query(None, description="Filter by shelf index"),
    cell_idx: Optional[int] = Query(None, description="Filter by cell index"),
    session: AsyncSession = Depends(get_db),
) -> List[StorageCellResponse]:
    """Get storage cells with optional filtering."""
    storage_service = StorageService(session)

    cells = await storage_service.get_storage_cells(shelf_idx=shelf_idx, cell_idx=cell_idx)

    return [
        StorageCellResponse(
            id=int(cell.id),
            shelf_idx=int(cell.shelf_idx),
            cell_idx=int(cell.cell_idx),
            capacity=_deserialize_capacity(str(cell.capacity)),
            status=str(cell.status),
            created_at=cell.created_at.isoformat(),
            updated_at=cell.updated_at.isoformat(),
        )
        for cell in cells
    ]


@router.put("/locations/{shelf_idx}/{cell_idx}", dependencies=[Depends(require_permissions("storage:write"))])
async def update_storage_cell(
    shelf_idx: int,
    cell_idx: int,
    cell_update: StorageCellUpdate,
    session: AsyncSession = Depends(get_db),
) -> StorageCellResponse:
    """Update storage cell status or capacity."""
    storage_service = StorageService(session)

    status_value = cell_update.status.lower() if cell_update.status else None
    success = await storage_service.update_storage_cell(
        shelf_idx=shelf_idx,
        cell_idx=cell_idx,
        status=status_value,
        capacity=_serialize_capacity(cell_update.capacity),
    )

    if not success:
        raise HTTPException(status_code=404, detail="Storage cell not found")

    cells = await storage_service.get_storage_cells(shelf_idx=shelf_idx, cell_idx=cell_idx)
    if not cells:
        raise HTTPException(status_code=404, detail="Storage cell not found")

    cell = cells[0]
    return StorageCellResponse(
        id=int(cell.id),
        shelf_idx=int(cell.shelf_idx),
        cell_idx=int(cell.cell_idx),
        capacity=_deserialize_capacity(str(cell.capacity)),
        status=str(cell.status),
        created_at=cell.created_at.isoformat(),
        updated_at=cell.updated_at.isoformat(),
    )


@router.put("/locations/batch-status", dependencies=[Depends(require_permissions("storage:write"))])
async def batch_update_storage_cell_status(
    payload: BatchStatusUpdate,
) -> Dict[str, Any]:
    from perpetual_os1.storage import CellStatus, StorageGrid, StorageModuleSide

    try:
        cell_status = CellStatus(payload.status.lower())
    except ValueError:
        valid = [s.value for s in CellStatus]
        raise HTTPException(
            status_code=422,
            detail=f"Status must be one of: {', '.join(valid)}",
        )

    try:
        grid = StorageGrid.load_from_file()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load storage grid: {exc}",
        ) from exc

    for cell_id in payload.cells:
        side_enum = StorageModuleSide.LEFT if cell_id.side == "left_module" else StorageModuleSide.RIGHT
        shelves = grid.get_side(side_enum)

        if cell_id.shelf_idx < 0 or cell_id.shelf_idx >= len(shelves):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid shelf_idx: {cell_id.shelf_idx}",
            )

        shelf = shelves[cell_id.shelf_idx]

        if cell_id.cell_idx < 0 or cell_id.cell_idx >= len(shelf.cells):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid cell_idx: {cell_id.cell_idx}",
            )

        shelf.cells[cell_id.cell_idx].status = cell_status

    grid.save()

    return {"updated": len(payload.cells)}
