"""Storage management endpoints with authorization."""
from __future__ import annotations
from openshelf_os_server.core.manual_motion import robot_command_enqueue_guard
from openshelf_os_server.documentation import BooleanSuccess, MessageResponse, response_docs
from openshelf_os_server.core.mass import MASS_FIELDS, validate_mass


import functools
import json
import logging
import time
from typing import Any, Callable, Dict, List, Literal, Optional, Union, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field, model_validator

import openshelf_os.common as common
from openshelf_os.config.settings import CompartmentLayoutSettings, StorageBinDimensionsSettings
import openshelf_os_server.core.robot as robot_core

from ..database.models import (
    Item,
    ItemLocation,
    StorageBinItem,
    StorageBinWorkflow,
    User,
)
from ..core.bin_storage import BinStorageService, BinWorkflowConflict, ensure_worker_homed_for_bin_motion, ensure_worker_queue_empty_for_bin_start, is_io_box_occupied, is_reserved_bin_barcode
from ..core.mock_robot import get_mock_robot_runtime
from ..core.storage import StorageService
from ..core.validation import CellOccupiedError, ValidationError
from .auth_deps import (
    can_user_retrieve_item,
    get_current_user,
    get_relay_or_current_user,
    require_permissions,
    require_relay_or_permissions,
)
from ..database.session import get_db
from openshelf_os.storage import StorageGrid
from openshelf_os_server.core.storage_module import StorageModuleSideDto
from openshelf_os_server.settings import get_operating_mode

router = APIRouter(tags=["storage"])


# ---------------------------------------------------------------------------
# Helper models
# ---------------------------------------------------------------------------


class DimensionsPayload(BaseModel):
    """Dimensions in millimeters (width, height, length)."""

    width: float
    height: float
    length: float


class MassInput(BaseModel):
    tare_weight: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, description="Mass in grams. Omit to preserve; null clears this reference.")
    net_weight: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False, description="Contents mass in grams; requires known tare. Mutually exclusive with weight. Null clears current gross/net.", examples=[250.5])
    full_net_weight: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, description="Mass in grams. Omit to preserve; null clears this reference.")


class ItemCreate(MassInput):
    """Request payload for creating an item."""

    model_config = ConfigDict(populate_by_name=True)

    upca: str
    name: str = Field(max_length=255)
    dimensions: Optional[Dict[str, float]] = Field(default=None, description="Positive width, height, and length in millimeters; required when enrolling a new item.", examples=[{"width":10,"height":10,"length":10}])
    weight: Optional[float] = Field(default=None, description="Gross mass in grams. Do not combine with net_weight; null does not clear gross on PUT.", examples=[350.5])
    item_metadata: Optional[Dict[str, Any]] = Field(default=None, alias="item_metadata", description="Arbitrary item metadata; metadata is also accepted as an input alias. Replaces the object when supplied.")
    access_level_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict) and "metadata" in data and "item_metadata" not in data:
            data["item_metadata"] = data.pop("metadata")
        return data

    @model_validator(mode="after")
    def ensure_metadata(self) -> "ItemCreate":
        metadata_was_provided = "item_metadata" in self.model_fields_set
        if self.item_metadata is None:
            self.item_metadata = {}
        if not metadata_was_provided:
            self.__pydantic_fields_set__.discard("item_metadata")
        return self


class ItemUpdate(MassInput):
    """Request payload for updating an item."""

    model_config = ConfigDict(populate_by_name=True)

    name: Optional[str] = Field(default=None, max_length=255)
    dimensions: Optional[Dict[str, float]] = Field(default=None, description="Positive width, height, and length in millimeters; required when enrolling a new item.", examples=[{"width":10,"height":10,"length":10}])
    weight: Optional[float] = Field(default=None, description="Gross mass in grams. Do not combine with net_weight; null does not clear gross on PUT.", examples=[350.5])
    item_metadata: Optional[Dict[str, Any]] = Field(default=None, alias="item_metadata", description="Arbitrary item metadata; metadata is also accepted as an input alias. Replaces the object when supplied.")
    access_level_id: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_metadata(cls, data: Any) -> Any:
        if isinstance(data, dict) and "metadata" in data and "item_metadata" not in data:
            data["item_metadata"] = data.pop("metadata")
        return data


class ItemLocationCreate(BaseModel):
    """Request payload for assigning a location to an item."""

    side: StorageModuleSideDto
    shelf_idx: int
    cell_idx: int


class StorageCellUpdate(BaseModel):
    """Request payload for updating a storage cell."""

    capacity: Optional[Union[str, DimensionsPayload]] = None
    status: Optional[str] = None


class ItemWithLocations(BaseModel):
    """Response payload describing an item with its locations."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    id: int
    upca: str
    name: str
    dimensions: Dict[str, float]
    weight: Optional[float]
    tare_weight: Optional[float] = Field(default=None, description="Known empty-container mass in grams")
    net_weight: Optional[float] = Field(default=None, description="Gross minus tare in grams, or null if unknown or gross is below tare")
    full_net_weight: Optional[float] = Field(default=None, description="Full contents reference in grams; independent of current contents")
    metadata: Dict[str, Any] = Field(default_factory=dict)
    access_level_id: Optional[int]
    locations: List[Dict[str, Any]] = Field(description="Direct database locations, plus the bin home location when binned; use the bin workflow for binned items")
    created_at: str
    updated_at: str
    can_retrieve: bool
    bin: Optional[Dict[str, Any]] = Field(default=None, description="Bin membership and aggregate state; null for direct inventory")


class BinWorkflowCreate(BaseModel):
    operation: str = Field(pattern=r"^(store|retrieve)$")
    upca: str
    storage_mode: Optional[Literal["bin", "direct"]] = None


class BinWorkflowReturn(BaseModel):
    confirmed_item_ids: List[int]


class TrayBarcode(BaseModel):
    barcode: str = Field(min_length=1, max_length=50)


class StorageCellResponse(BaseModel):
    """Response payload for a storage cell."""

    id: int
    shelf_idx: int
    cell_idx: int
    capacity: DimensionsPayload
    status: str
    created_at: str
    updated_at: str


class InventoryPage(BaseModel):
    items: list[ItemWithLocations]
    total: int
    skip: int
    limit: int


class BinItemSummary(BaseModel):
    id: int
    upca: str
    name: str
    compartment: int | None = Field(
        default=None, ge=1, le=1024,
        description="Compartment number, starting at 1 at the top left, left-to-right then top-to-bottom at IO; null when unassigned or the bin is undivided.",
    )


class BinResponse(BaseModel):
    id: int
    barcode: str
    home_location: ItemLocationCreate
    inner_dimensions: dict[str, float]
    max_item_dimensions: dict[str, float]
    max_items: int
    compartment_layout: CompartmentLayoutSettings | None = Field(
        default=None, description="Equal-sized compartment rows and columns; null for an undivided bin.",
    )
    compartment_dimensions: StorageBinDimensionsSettings | None = Field(
        default=None, description="Compartment dimensions in millimeters: inner width divided by columns, inner length divided by rows, and full inner height; null for an undivided bin.",
    )
    item_count: int
    used_volume: float = Field(description="Occupied volume in cubic millimeters")
    available_volume: float = Field(description="Remaining volume in cubic millimeters")
    status: str
    gross_weight: float | None = Field(description="Bin plus contents in grams; only available at home")
    active: bool
    items: list[BinItemSummary]


class BinsResponse(BaseModel):
    bins: list[BinResponse]


class BinContentsResponse(BinItemSummary):
    confirmed: bool


class BinWorkflowResponse(BaseModel):
    id: str
    operation: str = Field(description="store, retrieve, or return")
    state: str = Field(description="Current tray interaction state")
    compartment: int | None = Field(
        default=None, ge=1, le=1024,
        description="Persisted target compartment number, starting at 1 at the top left, left-to-right then top-to-bottom at IO; null when no target is assigned or the bin is undivided.",
    )
    bin: BinResponse
    item: BinItemSummary | None
    error: str | None
    history: list[dict[str, Any]]
    resolution: dict[str, Any] | None
    created_at: str
    updated_at: str
    contents: list[BinContentsResponse] | None = None


class BinWorkflowEnvelope(BaseModel):
    workflow: BinWorkflowResponse | None


class BinWorkflowStartResponse(BinWorkflowEnvelope):
    storage_mode: Literal["direct", "bin"]


class BinWorkflowsResponse(BaseModel):
    workflows: list[BinWorkflowResponse]


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


def _build_item_payload(
    item: Item,
    locations: List[ItemLocation],
    current_user: User,
    bin_payload: Optional[Dict[str, Any]] = None,
) -> ItemWithLocations:
    serialized_locations = _serialize_locations(locations)
    if bin_payload is not None:
        home = bin_payload["home_location"]
        serialized_locations.append(
            {
                "id": -int(bin_payload["id"]),
                "item_id": item.id,
                **home,
                "created_at": item.created_at.isoformat()
                if item.created_at
                else "1970-01-01T00:00:00+00:00",
                "updated_at": item.updated_at.isoformat()
                if item.updated_at
                else "1970-01-01T00:00:00+00:00",
            }
        )
    return ItemWithLocations(
        id=int(item.id),
        upca=str(item.upca),
        name=str(item.name),
        dimensions=cast(Dict[str, float], item.dimensions),
        weight=float(item.weight) if item.weight is not None else None,
        tare_weight=item.tare_weight,
        net_weight=item.net_weight,
        full_net_weight=item.full_net_weight,
        metadata=cast(Dict[str, Any], item.item_metadata) or {},
        access_level_id=item.access_level_id,
        locations=serialized_locations,
        created_at=item.created_at.isoformat() if item.created_at else "",
        updated_at=item.updated_at.isoformat() if item.updated_at else "",
        can_retrieve=can_user_retrieve_item(current_user, item) and (bin_payload is None or bin_payload["status"] == "at_home"),
        bin=bin_payload,
    )


async def _build_item_payload_with_bin(
    item: Item,
    locations: List[ItemLocation],
    current_user: User,
    session: AsyncSession,
) -> ItemWithLocations:
    bin_payload = await BinStorageService(session).item_bin_payload(item.id)
    return _build_item_payload(item, locations, current_user, bin_payload)


def _serialize_capacity(capacity: Union[str, DimensionsPayload, None]) -> Optional[str]:
    if capacity is None:
        return None
    if isinstance(capacity, str):
        return capacity
    return f"{capacity.width}x{capacity.height}x{capacity.length}"


def _deserialize_capacity(capacity: str) -> DimensionsPayload:
    stripped = capacity.strip()
    if stripped == "":
        return DimensionsPayload(width=0.0, height=0.0, length=0.0)

    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) is False:
                return DimensionsPayload(width=0.0, height=0.0, length=0.0)
            return DimensionsPayload(
                width=float(data.get("width", 0.0)),
                height=float(data.get("height", 0.0)),
                length=float(data.get("length", 0.0)),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return DimensionsPayload(width=0.0, height=0.0, length=0.0)

    parts = stripped.lower().split("x")
    if len(parts) != 3:
        return DimensionsPayload(width=0.0, height=0.0, length=0.0)

    try:
        width, height, length = (float(part.strip()) for part in parts)
    except ValueError:
        return DimensionsPayload(width=0.0, height=0.0, length=0.0)

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
    return await _build_item_payload_with_bin(
        item, locations, current_user, storage_service.session
    )


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


@router.get(
    "/bins",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="List storage bins",
    responses=response_docs(model=BinsResponse, errors=(401, 403)),
)
async def get_bins(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """List storage bins.

    Synchronizes configured bins into the database, then returns bin state and contents.
    Aggregate `gross_weight` includes the bin and all its contents; it is exposed only at
    home and is not an individual item mass.
    """
    service = BinStorageService(session)
    await service.sync_config()
    return {"bins": await service.list_bins()}


@router.get("/bin-workflows/active", responses=response_docs(model=BinWorkflowsResponse, errors=(401, 403)), dependencies=[Depends(require_relay_or_permissions("storage:read"))])
async def get_active_bin_workflow(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """List unfinished take, place, and return interactions. Outside trays do not reserve the robot; completed interactions are omitted."""
    service = BinStorageService(session)
    return {"workflows": [await service.serialize_workflow(w) for w in await service.active_workflows()]}


@router.post(
    "/bin-workflows",
    dependencies=[Depends(require_relay_or_permissions("storage:write"))],
    summary="Start a bin storage workflow",
    responses=response_docs(model=BinWorkflowStartResponse, errors=(401, 403, 404, 409)),
)
async def create_bin_workflow(
    request: BinWorkflowCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> Dict[str, Any]:
    """Start a bin storage workflow.

    Chooses storage for the requested item and operation. A `storage_mode=direct` response
    with `workflow=null` means no tray workflow was started; submit an ordinary command
    separately. A tray result starts delivery to the IO box. Retrieval
    requires access to every item in that bin. Clear the IO box first; a tray or item already
    in use, or an unavailable robot worker, returns `409`.
    """
    storage = StorageService(session)
    service = BinStorageService(session)
    await service.sync_config()
    item = await storage.get_item(request.upca)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    direct_locations = await storage.get_item_locations(item.upca)
    if request.operation == "store" and direct_locations:
        raise HTTPException(status_code=409, detail="Item is already stored")
    if request.operation == "store" and request.storage_mode == "direct":
        return {"storage_mode": "direct", "workflow": None}
    if request.operation == "retrieve" and await service.item_bin(item.id) is None:
        return {"storage_mode": "direct", "workflow": None}

    if request.operation == "retrieve":
        bin_row = await service.item_bin(item.id)
        assert bin_row is not None
        bin_items = (
            (
                await session.execute(
                    select(Item)
                    .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                    .where(StorageBinItem.bin_id == bin_row.id)
                )
            )
            .scalars()
            .all()
        )
        if any(not can_user_retrieve_item(current_user, candidate) for candidate in bin_items):
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to retrieve every item in this bin",
            )

    workflow = None
    worker = robot_core.robot_worker
    if get_operating_mode() == common.OperatingMode.mock:
        try:
            async with robot_command_enqueue_guard():
                if await get_mock_robot_runtime().has_active_commands():
                    raise BinWorkflowConflict("Robot has queued commands")
                workflow = await service.begin(request.operation, item, current_user.id)
                if workflow is not None:
                    await service.transition(workflow, "awaiting_operator", "mock tray delivered")
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if workflow is None:
            if request.operation == "store" and request.storage_mode == "bin":
                raise HTTPException(
                    status_code=409, detail="No compatible storage bin is available"
                )
            return {"storage_mode": "direct", "workflow": None}
        mock_runtime = get_mock_robot_runtime()
        mock_runtime.set_bin_io_box_occupied(True)
        await mock_runtime.publish_status()
        return {"storage_mode": "bin", "workflow": await service.serialize_workflow(workflow)}

    if worker is None:
        try:
            workflow = await service.begin(request.operation, item, current_user.id)
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if workflow is None:
            if request.operation == "store" and request.storage_mode == "bin":
                raise HTTPException(
                    status_code=409, detail="No compatible storage bin is available"
                )
            return {"storage_mode": "direct", "workflow": None}
        workflow.error = "Robot worker is unavailable"
        await service.fail(workflow, workflow.error)
    else:
        try:
            async with robot_command_enqueue_guard():
                if await is_io_box_occupied(worker):
                    raise HTTPException(
                        status_code=409,
                        detail="Remove the item from the IO box before retrieving the storage bin",
                    )
                ensure_worker_homed_for_bin_motion(worker)
                await ensure_worker_queue_empty_for_bin_start(worker)
                workflow = await service.begin(request.operation, item, current_user.id)
                if workflow is not None:
                    await service.start_retrieval(workflow, worker)
                if workflow is None:
                    if request.operation == "store" and request.storage_mode == "bin":
                        raise HTTPException(
                            status_code=409, detail="No compatible storage bin is available"
                        )
                    return {"storage_mode": "direct", "workflow": None}
            try:
                await service.publish_workflow(worker, workflow)
            except Exception as publish_error:
                logging.warning("Failed to publish bin workflow start: %s", publish_error)
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            if workflow is not None:
                workflow.error = str(exc)
                await service.fail(workflow, workflow.error)
            else:
                raise
    return {"storage_mode": "bin", "workflow": await service.serialize_workflow(workflow)}


async def _tray_workflow(session: AsyncSession, workflow_id: str) -> StorageBinWorkflow:
    workflow = await session.get(StorageBinWorkflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Tray workflow not found")
    return workflow


@router.post("/bin-workflows/returns", responses=response_docs(model=BinWorkflowEnvelope, errors=(401, 403, 404, 409, 422)), dependencies=[Depends(require_permissions("storage:write"))])
async def begin_tray_return(request: TrayBarcode, session: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Resolve a tray barcode and open contents selection without moving hardware. All known contents are selected by default, including the checkout target. An already stored tray returns 409."""
    service = BinStorageService(session)
    try:
        workflow = await service.begin_return(request.barcode, current_user.id)
    except BinWorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if workflow is None:
        raise HTTPException(status_code=404, detail="Tray barcode not found")
    if get_operating_mode() == common.OperatingMode.mock:
        runtime = get_mock_robot_runtime()
        runtime.set_bin_io_box_occupied(True)
        await runtime.publish_status()
    elif robot_core.robot_worker is not None:
        await service.publish_workflow(robot_core.robot_worker, workflow)
    return {"workflow": await service.serialize_workflow(workflow)}


@router.post("/bin-workflows/{workflow_id}/close", responses=response_docs(model=BinWorkflowEnvelope, errors=(401, 403, 404, 409, 422)), dependencies=[Depends(require_permissions("storage:write"))])
async def close_tray_workflow(workflow_id: str, session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Finish a take or place instruction and leave its tray outside. Closing a return dialog cancels that return without changing contents or moving hardware. No IO occupancy is required."""
    service = BinStorageService(session)
    workflow = await _tray_workflow(session, workflow_id)
    try:
        await service.close(workflow)
    except BinWorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if get_operating_mode() == common.OperatingMode.mock:
        runtime = get_mock_robot_runtime()
        runtime.set_bin_io_box_occupied(False)
        await runtime.publish_status()
    elif robot_core.robot_worker is not None:
        await service.publish_workflow(robot_core.robot_worker, workflow)
    return {"workflow": await service.serialize_workflow(workflow)}


@router.post("/bin-workflows/{workflow_id}/store", responses=response_docs(model=BinWorkflowEnvelope, errors=(401, 403, 404, 409, 422)), dependencies=[Depends(require_permissions("storage:write"))])
async def store_returned_tray(workflow_id: str, request: BinWorkflowReturn,
                              session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Submit selected known contents and return the tray from the IO bay to its shelf. Requires an occupied IO bay and an available, homed robot. Repeated submission of the same selection does not repeat motion."""
    service = BinStorageService(session)
    workflow = await _tray_workflow(session, workflow_id)
    worker = robot_core.robot_worker
    mocking = get_operating_mode() == common.OperatingMode.mock
    try:
        async with robot_command_enqueue_guard():
            if workflow.state == "awaiting_operator":
                if mocking:
                    if await get_mock_robot_runtime().has_active_commands():
                        raise BinWorkflowConflict("Robot has queued commands")
                    if not get_mock_robot_runtime().is_bin_io_box_occupied():
                        raise BinWorkflowConflict("Place the tray in the IO box before storing it")
                else:
                    if worker is None:
                        raise BinWorkflowConflict("Robot worker is unavailable")
                    ensure_worker_homed_for_bin_motion(worker)
                    await ensure_worker_queue_empty_for_bin_start(worker)
                    if not await is_io_box_occupied(worker):
                        raise BinWorkflowConflict("Place the tray in the IO box before storing it")
            if await service.prepare_return(workflow, request.confirmed_item_ids):
                if mocking:
                    await service.complete_return(workflow)
                    runtime = get_mock_robot_runtime()
                    runtime.set_bin_io_box_occupied(False)
                    await runtime.publish_status()
                else:
                    assert worker is not None
                    try:
                        await service.enqueue_return(workflow, worker)
                    except Exception as exc:
                        await service.fail(workflow, str(exc))
                        raise
    except BinWorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if worker is not None:
        await service.publish_workflow(worker, workflow)
    return {"workflow": await service.serialize_workflow(workflow)}


@router.get(
    "/items",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="List inventory records",
    responses=response_docs(model=InventoryPage, errors=(401, 403)),
)
@monitor_performance
async def get_items(
    response: Response,
    skip: int = 0,
    limit: int = 50,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> Dict[str, Any]:
    """List inventory records.

    Returns an offset page, total count, and matching `X-Total-Count`, `X-Limit`, and
    `X-Skip` headers. Records include access-based `can_retrieve`, current mass, and direct
    or bin-derived locations. A bin home location is not directly retrievable: use the bin
    workflow when `bin` is non-null.
    """
    storage_service = StorageService(session)

    items = await storage_service.get_all_items(skip=skip, limit=limit)
    total = await storage_service.get_items_count()

    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Limit"] = str(limit)
        response.headers["X-Skip"] = str(skip)

    item_payloads = [
        await _build_item_payload_with_bin(item, list(item.locations), current_user, session)
        for item in items
    ]

    return {
        "items": item_payloads,
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get(
    "/items/{upca}",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="Read an item and its storage state",
    responses=response_docs(errors=(401, 403, 404)),
)
async def get_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> ItemWithLocations:
    """Read an item and its storage state.

    Returns the inventory record, computed retrieval access, locations, and optional bin
    membership. Read this before moving an item. `net_weight` is derived from known gross
    and tare; it is null when gross is below tare or either value is missing. A missing
    record returns `404`.
    """
    storage_service = StorageService(session)

    item = await storage_service.get_item(upca)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(upca)
    return await _build_item_payload_with_bin(item, locations, current_user, session)


@router.post(
    "/items",
    dependencies=[Depends(require_relay_or_permissions("storage:write"))],
    summary="Create or update an inventory record",
    responses=response_docs(errors=(400, 401, 403, 409, 422)),
)
async def create_item(
    item: ItemCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> ItemWithLocations:
    """Create or update an inventory record.

    Creates a record or updates the existing UPCA. This does not move the item or assign
    physical occupancy. New records require valid dimensions; values are millimeters and
    masses are grams. Both `metadata` and `item_metadata` inputs are accepted; responses use
    `metadata`.

    To enroll an opened container, supply `tare_weight`, `net_weight`, and optionally
    `full_net_weight`; gross becomes tare plus net. Do not supply both `weight` and
    `net_weight`, even if one is null. See the storage group for mass persistence rules.
    Prefer PUT for partial edits to existing records. Reserved bin barcodes and forbidden
    changes to bin-managed items return `409`. A tray interaction holding the item does not
    block the edit; conflicting state is rejected when that interaction completes.
    """
    storage_service = StorageService(session)

    try:
        if await is_reserved_bin_barcode(item.upca, session):
            raise HTTPException(status_code=409, detail="Barcode is reserved for a storage bin")
        existing = await storage_service.get_item(item.upca)
        bin_service = BinStorageService(session)
        dimensions = item.dimensions
        metadata = item.item_metadata or {}
        access_level_id = item.access_level_id
        if existing is not None and await bin_service.item_bin(existing.id) is not None:
            provided_fields = item.model_fields_set
            dimensions_changed = (
                "dimensions" in provided_fields and item.dimensions != existing.dimensions
            )
            access_changed = (
                "access_level_id" in provided_fields
                and item.access_level_id != existing.access_level_id
            )
            if dimensions_changed or access_changed:
                raise HTTPException(
                    status_code=409,
                    detail="Retrieve the item before changing dimensions or access level",
                )
            if "dimensions" not in provided_fields:
                dimensions = existing.dimensions
            if "item_metadata" not in provided_fields:
                metadata = existing.item_metadata or {}
            if "access_level_id" not in provided_fields:
                access_level_id = existing.access_level_id
        db_item = await storage_service.create_item(
            upca=item.upca,
            name=item.name,
            dimensions=dimensions,
            weight=item.weight,
            preserve_existing_weight="weight" not in item.model_fields_set,
            metadata=metadata,
            access_level_id=access_level_id,
            mass_values={k: v for k, v in item.model_dump(exclude_unset=True).items() if k in (*MASS_FIELDS, "weight")},
        )

        locations = await storage_service.get_item_locations(item.upca)

        return await _build_item_payload_with_bin(db_item, locations, current_user, session)

    except HTTPException:
        raise
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put(
    "/items/{upca}",
    dependencies=[Depends(require_relay_or_permissions("storage:write"))],
    summary="Update selected inventory fields",
    responses=response_docs(errors=(401, 403, 404, 409, 422)),
)
async def update_item(
    upca: str,
    item: ItemUpdate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> ItemWithLocations:
    """Update selected inventory fields.

    Omitted mass fields retain their values. `net_weight=null` clears gross and derived net;
    `tare_weight=null` or `full_net_weight=null` clears that reference. Legacy `weight=null`
    does not clear gross on PUT. Supplying both `weight` and `net_weight` returns `422`.
    Setting net requires a known tare, either stored or supplied.

    Changing tare alone preserves gross and recomputes net. Metadata replaces the object
    when provided. Bin-managed dimensions/access changes require retrieval first. Returns
    `404` for an absent UPCA. These edits do not move
    hardware.
    """
    storage_service = StorageService(session)

    existing = await storage_service.get_item(upca)
    bin_service = BinStorageService(session)
    if existing is not None and await bin_service.item_bin(existing.id) is not None:
        dimensions_changed = item.dimensions is not None and item.dimensions != existing.dimensions
        access_changed = (
            "access_level_id" in item.model_fields_set
            and item.access_level_id != existing.access_level_id
        )
        if dimensions_changed or access_changed:
            raise HTTPException(
                status_code=409,
                detail="Retrieve the item before changing dimensions or access level",
            )

    try:
        validate_mass(item.model_dump(exclude_unset=True), existing.tare_weight if existing else None)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    updated_item = await storage_service.update_item(
        upca=upca,
        name=item.name,
        dimensions=item.dimensions,
        weight=item.weight,
        metadata=item.item_metadata,
        access_level_id=item.access_level_id,
        mass_values={k: v for k, v in item.model_dump(exclude_unset=True).items() if k in (*MASS_FIELDS, "weight")},
    )

    if not updated_item:
        raise HTTPException(status_code=404, detail="Item not found")

    fresh_item = await storage_service.get_item(upca)
    if fresh_item is None:
        fresh_item = updated_item

    locations = await storage_service.get_item_locations(upca)

    return await _build_item_payload_with_bin(fresh_item, locations, current_user, session)


@router.delete(
    "/items/{upca}",
    dependencies=[Depends(require_relay_or_permissions("storage:delete"))],
    summary="Delete an inventory record",
    responses=response_docs(model=MessageResponse, errors=(401, 403, 404, 409)),
)
@monitor_performance
async def delete_item(
    upca: str,
    session: AsyncSession = Depends(get_db),
) -> Dict[str, str]:
    """Delete an inventory record.

    Deletes the item and its database location records; it does not physically retrieve it.
    Bin membership and retained workflow history block deletion. Use this low-level endpoint
    only after reconciling physical occupancy. A missing record returns `404`.
    """
    storage_service = StorageService(session)

    existing = await storage_service.get_item(upca)
    bin_service = BinStorageService(session)
    if existing is not None and await bin_service.item_bin(existing.id) is not None:
        raise HTTPException(status_code=409, detail="Retrieve the item before deleting it")
    if existing is not None and await bin_service.item_has_workflow_history(existing.id):
        raise HTTPException(
            status_code=409,
            detail="Item cannot be deleted because storage bin workflow history is retained",
        )

    deleted = await storage_service.delete_item(upca)

    if deleted is False:
        raise HTTPException(status_code=404, detail="Item not found")

    return {"message": "Item deleted successfully"}


# ---------------------------------------------------------------------------
# Item location endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/items/{upca}/locations",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Assign an inventory location",
    responses=response_docs(errors=(400, 401, 403, 404, 409, 422)),
)
async def add_item_location(
    upca: str,
    location: ItemLocationCreate,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Assign an inventory location.

    Adds database occupancy for an item without moving hardware. Side and zero-based
    shelf/cell indices identify the location. Occupied cells and incompatible bin state are
    rejected. Tray homes are usable while their tray is away; the tray's return then reports
    the occupied home instead. The status stream overlays these database locations on the
    grid.
    """
    storage_service = StorageService(session)

    db_item = await storage_service.get_item(upca)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    bin_service = BinStorageService(session)
    if await bin_service.item_bin(db_item.id) is not None:
        raise HTTPException(status_code=409, detail="Item is stored in a bin")

    try:
        storage_location = await storage_service.add_item_location(
            upca=upca,
            side=location.side,
            shelf_idx=location.shelf_idx,
            cell_idx=location.cell_idx,
        )
    except CellOccupiedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not storage_location:
        raise HTTPException(status_code=400, detail="Failed to add location")

    return await _load_item_with_locations(upca, storage_service, current_user)


async def _remove_item_location_impl(
    upca: str,
    side: StorageModuleSideDto,
    shelf_idx: int,
    cell_idx: int,
    storage_service: StorageService,
) -> None:
    item = await storage_service.get_item(upca)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    bin_service = BinStorageService(storage_service.session)
    if await bin_service.item_bin(item.id) is not None:
        raise HTTPException(status_code=409, detail="Item is stored in a bin")
    success = await storage_service.remove_item_location(
        upca=upca,
        side=side,
        shelf_idx=shelf_idx,
        cell_idx=cell_idx,
    )

    if not success:
        raise HTTPException(status_code=404, detail="Location not found")


@router.delete(
    "/items/{upca}/locations",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Remove an inventory location",
    responses=response_docs(errors=(401, 403, 404, 409)),
)
async def remove_item_location(
    upca: str,
    side: StorageModuleSideDto,
    shelf_idx: int,
    cell_idx: int,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Remove an inventory location.

    Removes the specified database location using query parameters. This does not move
    hardware or remove the storage-grid cell. Use only when physical occupancy has already
    been reconciled.
    """
    storage_service = StorageService(session)

    await _remove_item_location_impl(upca, side, shelf_idx, cell_idx, storage_service)
    return await _load_item_with_locations(upca, storage_service, current_user)


@router.delete(
    "/items/{upca}/locations/{side}/{shelf_idx}/{cell_idx}",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Remove an inventory location by path",
    responses=response_docs(errors=(401, 403, 404, 409)),
)
async def remove_item_location_by_path(
    upca: str,
    side: StorageModuleSideDto,
    shelf_idx: int,
    cell_idx: int,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Remove an inventory location by path.

    Equivalent to the query-parameter removal endpoint, with side and zero-based shelf/cell
    indices in the URL. Updates database occupancy only; no robot command is issued.
    """
    storage_service = StorageService(session)

    await _remove_item_location_impl(upca, side, shelf_idx, cell_idx, storage_service)
    return await _load_item_with_locations(upca, storage_service, current_user)


# ---------------------------------------------------------------------------
# Storage cell endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/locations",
    dependencies=[Depends(require_permissions("storage:read"))],
    summary="List storage cell records",
    responses=response_docs(errors=(401, 403)),
)
async def get_storage_cells(
    shelf_idx: Optional[int] = Query(None, description="Filter by shelf index"),
    cell_idx: Optional[int] = Query(None, description="Filter by cell index"),
    session: AsyncSession = Depends(get_db),
) -> List[StorageCellResponse]:
    """List storage cell records.

    Returns database cell records, optionally filtered by zero-based shelf and cell index.
    These records are separate from the robot storage-grid file and item-location occupancy.
    """
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


@router.put(
    "/locations/{shelf_idx}/{cell_idx}",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Update a storage cell record",
    responses=response_docs(errors=(401, 403, 404)),
)
async def update_storage_cell(
    shelf_idx: int,
    cell_idx: int,
    cell_update: StorageCellUpdate,
    session: AsyncSession = Depends(get_db),
) -> StorageCellResponse:
    """Update a storage cell record.

    Updates database status or capacity for the selected cell. Capacity accepts a dimension
    object or a width×height×length string using `x` separators. Valid states are available,
    occupied, maintenance, and disabled. This does not move hardware or rewrite the robot
    grid.
    """
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


def _get_robot_grid():
    if robot_core.robot_worker is not None and robot_core.robot_worker._robot._grid is not None:
        return robot_core.robot_worker._robot._grid

    config_path = common.CONFIG_PATH
    config = common.load_config_file(config_path)
    grid_path = common.resolve_grid_path(config, config_path)

    return StorageGrid.load_from_file(grid_path)


@router.get(
    "/grid",
    dependencies=[Depends(require_permissions("storage:read"))],
    summary="Read the robot storage grid",
    responses=response_docs(model=StorageGrid, errors=(401, 403, 500)),
)
async def get_storage_grid() -> Dict[str, Any]:
    """Read the robot storage grid.

    Returns the robot storage-grid structure and calibration data. This is separate from
    item-location database records; use the status stream for database-overlaid occupancy.
    """
    try:
        grid = _get_robot_grid()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load storage grid: {exc}",
        ) from exc

    return grid.model_dump()


@router.delete(
    "/grid/shelf",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Delete a shelf from the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 500)),
)
async def delete_shelf(
    side: StorageModuleSideDto = Query(...), shelf_idx: int = Query(...)
) -> Dict[str, Any]:
    """Delete a shelf from the robot grid.

    Deletes the selected side/shelf and its cells from the persisted grid, without
    retrieving physical contents. This engineering operation changes storage geometry, not
    item inventory.
    """
    try:
        grid = _get_robot_grid()
        grid.remove_shelf(side, shelf_idx)
        return {"success": True}
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete shelf: {exc}",
        ) from exc


@router.delete(
    "/grid/cell",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Delete a cell from the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 500)),
)
async def delete_cell(
    side: StorageModuleSideDto = Query(...), shelf_idx: int = Query(...), cell_idx: int = Query(...)
) -> Dict[str, Any]:
    """Delete a cell from the robot grid.

    Deletes one side/shelf/cell from the persisted grid, without moving hardware. Reconcile
    item occupancy separately before changing storage geometry.
    """
    try:
        grid = _get_robot_grid()
        grid.remove_cell(side, shelf_idx, cell_idx)
        return {"success": True}
    except IndexError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete cell: {exc}",
        ) from exc
