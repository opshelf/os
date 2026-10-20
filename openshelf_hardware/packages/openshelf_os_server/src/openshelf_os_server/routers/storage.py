"""Storage management endpoints with authorization."""
from __future__ import annotations
from openshelf_os_server.core.manual_motion import robot_command_enqueue_guard
from openshelf_os_server.documentation import BooleanSuccess, MessageResponse, response_docs
from openshelf_os_server.core.mass import MASS_FIELDS, validate_mass


import functools
import json
import logging
import time
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Union, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, ConfigDict, Field, model_validator

import openshelf_os.common as common
from openshelf_os.config.settings import (
    BARCODE_MAX_LENGTH,
    CompartmentLayoutSettings,
    StorageBinDimensionsSettings,
    StorageBinLocationSettings,
    StorageBinSettings,
)
import openshelf_os_server.core.robot as robot_core

from ..database.models import (
    Item,
    ItemLocation,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
    User,
)
from ..core.bin_storage import ACTIVE_STATES, BATCH_OPERATION, BinStorageService, BinWorkflowConflict, NoCompatibleBinError, ensure_worker_homed_for_bin_motion, ensure_worker_queue_empty_for_bin_start, is_io_box_occupied, is_reserved_bin_barcode
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
from .calibration import ensure_laser_auto_calibration_not_running
from ..database.session import get_db
from openshelf_os.storage import (
	CalibrationCoordinate,
	Cell,
	CellStatus,
	Dimensions,
	StorageGrid,
	StorageModuleCellLocation,
	StorageModuleSide,
)
from openshelf_os_server.core.storage_module import StorageModuleSideDto, side_to_s
from openshelf_os_server.errors import api_error
from openshelf_os_server.settings import clear_config_cache, get_operating_mode, get_root_config, save_root_config

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

    barcode: str
    name: str = Field(max_length=255)
    dimensions: Optional[Dict[str, float]] = Field(default=None, description="Positive width, height, and length in millimeters; required when enrolling a new item.", examples=[{"width":10,"height":10,"length":10}])
    weight: Optional[float] = Field(default=None, description="Gross mass in grams. Do not combine with net_weight; null does not clear stored gross.", examples=[350.5])
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
    weight: Optional[float] = Field(default=None, description="Gross mass in grams. Do not combine with net_weight; null does not clear stored gross.", examples=[350.5])
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


class BarcodeRequest(BaseModel):
    """Request payload identifying one item by barcode."""

    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH)


class ItemUpdateRequest(ItemUpdate):
    """Request payload for updating one item."""

    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH)


class ItemLocationRequest(ItemLocationCreate):
    """Request payload for assigning or removing one item location."""

    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH)


class StorageCellUpdate(BaseModel):
    """Request payload for updating a storage cell."""

    capacity: Optional[Union[str, DimensionsPayload]] = None
    status: Optional[str] = None


class GridCellUpdate(BaseModel):
    """Request payload for updating a robot storage-grid cell."""

    x: Optional[float] = Field(default=None, description="Calibration x in millimeters; provide x, y, and z_offset together.")
    y: Optional[float] = Field(default=None, description="Calibration y in millimeters; provide x, y, and z_offset together.")
    z_offset: Optional[float] = Field(default=None, description="Calibration z offset in millimeters; provide x, y, and z_offset together.")
    status: Optional[str] = Field(default=None, description="Cell status: available, occupied, maintenance, or disabled.")
    is_manual: Optional[bool] = Field(default=None, description="Whether the calibration is locked (manual); omit to keep the current value.")
    dimensions: Optional[DimensionsPayload] = Field(default=None, description="Cell width, height, and length in millimeters; omit to keep the current dimensions.")


class GridCellBatchItem(GridCellUpdate):
    """One cell to update in an atomic multi-cell update."""

    shelf_idx: int
    cell_idx: int


class GridCellsBatchUpdate(BaseModel):
    """Request payload for an atomic multi-cell update."""

    cells: List[GridCellBatchItem]


class ItemWithLocations(BaseModel):
    """Response payload describing an item with its locations."""

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    id: int
    barcode: str
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
    barcode: str
    storage_mode: Optional[Literal["bin", "direct"]] = None


class BinWorkflowReturn(BaseModel):
    confirmed_item_ids: List[int]


class TrayBarcode(BaseModel):
    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH)


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
    barcode: str
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
        default=None, description="Compartment dimensions in millimeters: the inner width and length less the 2 mm dividers, split evenly by columns and rows, and the full inner height; null for an undivided bin.",
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


class UnresolvedTrayRecovery(BaseModel):
    workflow: BinWorkflowResponse
    error: str = Field(description="Why the tray stays outside; return it by scanning its barcode")


class TrayCrashRecoveryResponse(BaseModel):
    recovered: list[BinWorkflowResponse]
    unresolved: list[UnresolvedTrayRecovery]


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
        barcode=str(item.barcode),
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
    barcode: str,
    storage_service: StorageService,
    current_user: User,
) -> ItemWithLocations:
    item = await storage_service.get_item(barcode)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(barcode)
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


def _tray_config_error(exc: ValueError) -> HTTPException:
    logging.warning("Storage bin config sync rejected: %s", exc)
    return api_error(
        409,
        "tray_config_conflict",
        "",
    )


def _grid_load_error(exc: Exception) -> HTTPException:
    logging.error("Storage grid load failed: %s", exc)
    return api_error(500, "grid_load_failed", "The shelf layout could not be read. Try again.")


def _grid_update_error(exc: Exception) -> HTTPException:
    logging.error("Storage grid update failed: %s", exc)
    return api_error(500, "grid_update_failed", "The shelf layout could not be saved. Try again.")


def _grid_location_error(exc: IndexError) -> HTTPException:
    logging.info("Storage grid location not found: %s", exc)
    return api_error(404, "cell_not_found", "")


def _is_admin(user: User) -> bool:
    # System identities (the relay service, the developer user) are admins too, but the
    # unsafe edits of a bin member are for a person at the machine.
    return user.role.name == "admin" and getattr(user, "is_system", False) is not True


def _denied_item_names(items: Sequence[Item]) -> str:
    names = [item.name for item in items]
    if len(names) > 3:
        names = [*names[:3], f"and {len(names) - 3} more"]
    return ", ".join(names)


# ---------------------------------------------------------------------------
# Item endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/bins",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="List storage bins",
    responses=response_docs(model=BinsResponse, errors=(401, 403, 409)),
)
async def get_bins(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """List storage bins.

    Synchronizes configured bins into the database, then returns bin state and contents.
    A configured bin barcode already assigned to an item returns `409`.
    Aggregate `gross_weight` includes the bin and all its contents; it is exposed only at
    home and is not an individual item mass.
    """
    service = BinStorageService(session)
    try:
        await service.sync_config()
    except ValueError as exc:
        raise _tray_config_error(exc) from exc
    return {"bins": await service.list_bins()}


class TrayLayoutUpdate(BaseModel):
    """Request payload for redefining a storage bin's compartment grid."""

    rows: int = Field(ge=1, le=32, strict=True)
    columns: int = Field(ge=1, le=32, strict=True)


@router.put(
    "/bins/tray-layout",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Update a storage bin's compartment layout",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 422, 409, 500)),
)
async def update_tray_layout(
    payload: TrayLayoutUpdate,
    side: StorageModuleSideDto = Query(...),
    shelf_idx: int = Query(...),
    cell_idx: int = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Redefine the rows/columns compartment grid of the tray homed at a cell.

    Writes the tray's ``compartment_layout`` (and its derived ``max_items``) into the
    config, which is the source of truth, then re-syncs the database. The tray must be
    empty, at home, and have no in-flight workflow; otherwise the change is refused.
    """
    config = get_root_config()
    home = (side, shelf_idx, cell_idx)
    entry = next(
        (
            bin_setting
            for bin_setting in config.storage.bins
            if (
                bin_setting.home_location.side,
                bin_setting.home_location.shelf_idx,
                bin_setting.home_location.cell_idx,
            )
            == home
        ),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="No bin is assigned to this cell.")

    current = entry.compartment_layout
    changed = current is None or current.rows != payload.rows or current.columns != payload.columns
    if changed:
        row = (
            (
                await session.execute(
                    select(StorageBin).where(
                        StorageBin.side == side,
                        StorageBin.shelf_idx == shelf_idx,
                        StorageBin.cell_idx == cell_idx,
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            service = BinStorageService(session)
            unfinished = await session.scalar(
                select(StorageBinWorkflow.id)
                .where(
                    StorageBinWorkflow.bin_id == row.id,
                    StorageBinWorkflow.state.in_(ACTIVE_STATES),
                )
                .limit(1)
            )
            if row.status != "at_home" or await service._member_count(row.id) or unfinished is not None:
                raise HTTPException(
                    status_code=409,
                    detail="Empty the bin and return it before changing its compartment layout.",
                )

    next_config = config.model_copy(deep=True)
    for bin_setting in next_config.storage.bins:
        if bin_setting.barcode == entry.barcode:
            bin_setting.compartment_layout = CompartmentLayoutSettings(rows=payload.rows, columns=payload.columns)
            bin_setting.max_items = payload.rows * payload.columns

    save_root_config(next_config)
    clear_config_cache()

    try:
        await BinStorageService(session).sync_config()
    except ValueError as exc:
        raise _tray_config_error(exc) from exc

    return {"success": True}


class TrayAssign(BaseModel):
    """Request payload for marking a cell as a multi-item tray home."""

    barcode: str = Field(min_length=1, max_length=BARCODE_MAX_LENGTH)
    rows: int = Field(ge=1, le=32, strict=True)
    columns: int = Field(ge=1, le=32, strict=True)


@router.post(
    "/bins/tray",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Mark a cell as a multi-item tray home",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 422, 409, 500)),
)
async def assign_tray(
    payload: TrayAssign,
    side: StorageModuleSideDto = Query(...),
    shelf_idx: int = Query(...),
    cell_idx: int = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Create a storage bin homed at a cell, making it a multi-item tray.

    The tray's inner dimensions default to the cell's own dimensions and its
    ``max_items`` is derived from ``rows * columns``. The cell must not already be a tray
    home, and the barcode must be unreserved by another tray or item.
    """
    try:
        grid = _get_robot_grid()
    except Exception as exc:
        raise _grid_load_error(exc) from exc

    cell = grid.get_cell_if_present(
        StorageModuleCellLocation(side=side, shelf_idx=shelf_idx, cell_idx=cell_idx)
    )
    if cell is None:
        raise HTTPException(status_code=404, detail="Cell does not exist in the grid")
    # Laser calibration leaves cells unsized; the tray inherits the cell's dimensions.
    if min(cell.dimensions.width, cell.dimensions.height, cell.dimensions.length) <= 0:
        raise HTTPException(
            status_code=409,
            detail="Set the cell's size before assigning a bin.",
        )

    config = get_root_config()
    home = (side, shelf_idx, cell_idx)
    if any(
        (
            bin_setting.home_location.side,
            bin_setting.home_location.shelf_idx,
            bin_setting.home_location.cell_idx,
        )
        == home
        for bin_setting in config.storage.bins
    ):
        raise HTTPException(status_code=409, detail="This cell is already assigned to a bin.")

    active_barcodes = (
        await session.execute(select(StorageBin.barcode).where(StorageBin.active.is_(True)))
    ).scalars()
    reserved = {bin_setting.barcode for bin_setting in config.storage.bins} | set(active_barcodes)
    if payload.barcode in reserved:
        raise HTTPException(status_code=409, detail="That barcode is already in use")
    existing_item = (
        await session.execute(select(Item).where(Item.barcode == payload.barcode))
    ).scalar_one_or_none()
    if existing_item is not None:
        raise HTTPException(status_code=409, detail="That barcode belongs to an item")

    stale_row = (
        (
            await session.execute(
                select(StorageBin).where(
                    StorageBin.side == side,
                    StorageBin.shelf_idx == shelf_idx,
                    StorageBin.cell_idx == cell_idx,
                )
            )
        )
        .scalars()
        .first()
    )
    if stale_row is not None:
        if stale_row.active:
            raise HTTPException(status_code=409, detail="This cell is already assigned to a bin.")
        other_holder_ids = (
            await session.execute(
                select(StorageBin.id).where(StorageBin.barcode == payload.barcode)
            )
        ).scalars().all()
        if any(row_id != stale_row.id for row_id in other_holder_ids):
            raise HTTPException(
                status_code=409,
                detail="That barcode is already used by another bin.",
            )
        # A prior, removed tray left a deactivated row occupying this home cell. Re-point it
        # at the new barcode so the re-sync updates it in place instead of colliding with
        # the unique home-cell constraint.
        stale_row.barcode = payload.barcode
        await session.commit()

    entry = StorageBinSettings(
        barcode=payload.barcode,
        home_location=StorageBinLocationSettings(side=side.value, shelf_idx=shelf_idx, cell_idx=cell_idx),
        inner_dimensions=StorageBinDimensionsSettings(
            width=cell.dimensions.width,
            height=cell.dimensions.height,
            length=cell.dimensions.length,
        ),
        max_items=payload.rows * payload.columns,
        compartment_layout=CompartmentLayoutSettings(rows=payload.rows, columns=payload.columns),
    )

    next_config = config.model_copy(deep=True)
    next_config.storage.bins = [*next_config.storage.bins, entry]
    save_root_config(next_config)
    clear_config_cache()

    try:
        await BinStorageService(session).sync_config()
    except ValueError as exc:
        raise _tray_config_error(exc) from exc

    return {"success": True}


@router.delete(
    "/bins/tray",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Remove a cell's multi-item tray",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 422, 409, 500)),
)
async def unassign_tray(
    side: StorageModuleSideDto = Query(...),
    shelf_idx: int = Query(...),
    cell_idx: int = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Remove the storage bin homed at a cell, freeing the cell.

    The tray must be empty and at home with no in-flight workflow; otherwise it is refused.
    The bin is dropped from the config, the source of truth, and its database row is
    deactivated on re-sync.
    """
    config = get_root_config()
    home = (side, shelf_idx, cell_idx)
    entry = next(
        (
            bin_setting
            for bin_setting in config.storage.bins
            if
            (
                bin_setting.home_location.side,
                bin_setting.home_location.shelf_idx,
                bin_setting.home_location.cell_idx,
            )
            == home
        ),
        None,
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="No bin is assigned to this cell.")

    row = (
        (await session.execute(select(StorageBin).where(StorageBin.barcode == entry.barcode)))
        .scalars()
        .first()
    )
    if row is not None:
        service = BinStorageService(session)
        unfinished = await session.scalar(
            select(StorageBinWorkflow.id)
            .where(
                StorageBinWorkflow.bin_id == row.id,
                StorageBinWorkflow.state.in_(ACTIVE_STATES),
            )
            .limit(1)
        )
        if row.status != "at_home" or await service._member_count(row.id) or unfinished is not None:
            raise HTTPException(
                status_code=409,
                detail="Empty the bin and return it before removing it.",
            )

    next_config = config.model_copy(deep=True)
    next_config.storage.bins = [
        bin_setting
        for bin_setting in next_config.storage.bins
        if bin_setting.barcode != entry.barcode
    ]
    save_root_config(next_config)
    clear_config_cache()

    try:
        await BinStorageService(session).sync_config()
    except ValueError as exc:
        raise _tray_config_error(exc) from exc

    return {"success": True}


@router.get("/bin-workflows/active", responses=response_docs(model=BinWorkflowsResponse, errors=(401, 403)), dependencies=[Depends(require_relay_or_permissions("storage:read"))])
async def get_active_bin_workflow(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """List unfinished take, place, and return interactions. Outside trays do not reserve the robot; completed interactions are omitted, and so are import trays, which the import screen drives."""
    service = BinStorageService(session)
    return {
        "workflows": [
            await service.serialize_workflow(w)
            for w in await service.active_workflows()
            if w.operation != BATCH_OPERATION
        ]
    }


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
    try:
        await service.sync_config()
    except ValueError as exc:
        raise _tray_config_error(exc) from exc
    item = await storage.get_item(request.barcode)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")

    direct_locations = await storage.get_item_locations(item.barcode)
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
        denied = [
            candidate
            for candidate in bin_items
            if can_user_retrieve_item(current_user, candidate) is False
        ]
        if denied:
            raise api_error(
                403,
                "bin_access_denied",
                f"This bin also holds items you cannot access: "
                f"{_denied_item_names(denied)}. Ask a supervisor to retrieve it.",
            )

    workflow = None
    worker = robot_core.robot_worker
    if get_operating_mode() == common.OperatingMode.mock:
        try:
            async with robot_command_enqueue_guard():
                if await get_mock_robot_runtime().has_active_commands():
                    raise BinWorkflowConflict("The robot has other tasks waiting.")
                workflow = await service.begin(request.operation, item, current_user.id)
                if workflow is not None:
                    await service.transition(workflow, "awaiting_operator", "mock bin delivered")
        except NoCompatibleBinError as exc:
            # Without an explicit bin request the item may still go to its own cell.
            if request.storage_mode != "bin":
                return {"storage_mode": "direct", "workflow": None}
            raise api_error(409, "no_compatible_bin", str(exc)) from exc
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if workflow is None:
            return {"storage_mode": "direct", "workflow": None}
        mock_runtime = get_mock_robot_runtime()
        mock_runtime.set_bin_io_box_occupied(True)
        await mock_runtime.publish_status()
        return {"storage_mode": "bin", "workflow": await service.serialize_workflow(workflow)}

    if worker is None:
        try:
            workflow = await service.begin(request.operation, item, current_user.id)
        except NoCompatibleBinError as exc:
            # Without an explicit bin request the item may still go to its own cell.
            if request.storage_mode != "bin":
                return {"storage_mode": "direct", "workflow": None}
            raise api_error(409, "no_compatible_bin", str(exc)) from exc
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if workflow is None:
            return {"storage_mode": "direct", "workflow": None}
        workflow.error = "The robot is not running."
        await service.fail(workflow, workflow.error, tray_moved=False)
    else:
        try:
            async with robot_command_enqueue_guard():
                if await is_io_box_occupied(worker):
                    raise api_error(
                        409,
                        "io_box_occupied",
                        "The IO box is not empty. Take the item off the scale and wait for it to read empty.",
                    )
                ensure_worker_homed_for_bin_motion(worker)
                await ensure_worker_queue_empty_for_bin_start(worker)
                workflow = await service.begin(request.operation, item, current_user.id)
                if workflow is not None:
                    await service.start_retrieval(workflow, worker)
                else:
                    # A store without a bin raised NoCompatibleBinError above; a
                    # retrieve can lose its membership to a concurrent edit.
                    return {"storage_mode": "direct", "workflow": None}
            try:
                await service.publish_workflow(worker, workflow)
            except Exception as publish_error:
                logging.warning("Failed to publish bin workflow start: %s", publish_error)
        except NoCompatibleBinError as exc:
            # Without an explicit bin request the item may still go to its own cell.
            if request.storage_mode != "bin":
                return {"storage_mode": "direct", "workflow": None}
            raise api_error(409, "no_compatible_bin", str(exc)) from exc
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            if workflow is not None:
                workflow.error = str(exc)
                await service.fail(workflow, workflow.error, tray_moved=False)
            else:
                raise
    return {"storage_mode": "bin", "workflow": await service.serialize_workflow(workflow)}


@router.get("/bin-workflows/interrupted", responses=response_docs(model=BinWorkflowsResponse, errors=(401, 403)), dependencies=[Depends(require_permissions("storage:read"))])
async def get_crash_interrupted_bin_workflows(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """List tray motions a crash or emergency stop interrupted whose tray is still marked outside. The crash dialog names these trays and their home cells so the operator can put them back."""
    service = BinStorageService(session)
    return {"workflows": [await service.serialize_workflow(w) for w in await service.crash_interrupted_workflows()]}


@router.post("/bin-workflows/interrupted/recover", responses=response_docs(model=TrayCrashRecoveryResponse, errors=(401, 403)), dependencies=[Depends(require_permissions("storage:write"))])
async def recover_crash_interrupted_bin_workflows(session: AsyncSession = Depends(get_db)) -> Dict[str, Any]:
    """Mark trays from interrupted motions as home after the operator confirmed them on their shelves in the crash dialog. An interrupted return records the contents confirmed before it started. Does not move hardware. A tray whose state conflicts with being home stays outside and is listed as unresolved."""
    service = BinStorageService(session)
    recovered: list[Dict[str, Any]] = []
    unresolved: list[Dict[str, Any]] = []
    for workflow, error in await service.recover_crash_interrupted():
        payload = await service.serialize_workflow(workflow)
        if error is None:
            recovered.append(payload)
        else:
            unresolved.append({"workflow": payload, "error": error})
        if robot_core.robot_worker is not None:
            await service.publish_workflow(robot_core.robot_worker, workflow)
    return {"recovered": recovered, "unresolved": unresolved}


async def _tray_workflow(session: AsyncSession, workflow_id: str) -> StorageBinWorkflow:
    workflow = await session.get(StorageBinWorkflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="This bin task no longer exists.")
    return workflow


@router.post("/bin-workflows/returns", responses=response_docs(model=BinWorkflowEnvelope, errors=(401, 403, 404, 409, 422)), dependencies=[Depends(require_permissions("storage:write"))])
async def begin_tray_return(request: TrayBarcode, session: AsyncSession = Depends(get_db),
                            current_user: User = Depends(get_current_user)) -> Dict[str, Any]:
    """Resolve a tray barcode and open contents selection without moving hardware. Known contents are selected by default; an item just taken out is listed but unselected. An already stored tray returns 409."""
    service = BinStorageService(session)
    try:
        workflow = await service.begin_return(request.barcode, current_user.id)
    except BinWorkflowConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if workflow is None:
        raise HTTPException(status_code=404, detail="That barcode is not a bin.")
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
                        raise BinWorkflowConflict("The robot has other tasks waiting.")
                    if not get_mock_robot_runtime().is_bin_io_box_occupied():
                        raise BinWorkflowConflict("Place the bin in the IO box before returning it.")
                else:
                    if worker is None:
                        raise BinWorkflowConflict("The robot is not running.")
                    ensure_worker_homed_for_bin_motion(worker)
                    await ensure_worker_queue_empty_for_bin_start(worker)
                    if not await is_io_box_occupied(worker):
                        raise BinWorkflowConflict("Place the bin in the IO box before returning it.")
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
    # Resolve every page row's tray in one batch: per-item lookups re-read the same tray once
    # per member, which made this endpoint issue two queries per row.
    bin_payloads = await BinStorageService(session).item_bin_payloads(
        [int(item.id) for item in items]
    )

    if response is not None:
        response.headers["X-Total-Count"] = str(total)
        response.headers["X-Limit"] = str(limit)
        response.headers["X-Skip"] = str(skip)

    item_payloads = [
        _build_item_payload(
            item, list(item.locations), current_user, bin_payloads.get(int(item.id))
        )
        for item in items
    ]

    return {
        "items": item_payloads,
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.post(
    "/items/lookup",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="Read an item and its storage state",
    responses=response_docs(errors=(401, 403, 404)),
)
async def lookup_item(
    request: BarcodeRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> ItemWithLocations:
    """Read an item and its storage state.

    The barcode travels in the request body, so every customer barcode is addressable.
    Returns the inventory record, computed retrieval access, locations, and optional bin
    membership. Read this before moving an item. `net_weight` is derived from known gross
    and tare; it is null when gross is below tare or either value is missing. A missing
    record returns `404`.
    """
    storage_service = StorageService(session)
    barcode = request.barcode

    item = await storage_service.get_item(barcode)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    locations = await storage_service.get_item_locations(barcode)
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

    Creates a record or updates the existing barcode. This does not move the item or assign
    physical occupancy. New records require valid dimensions; values are millimeters and
    masses are grams. Both `metadata` and `item_metadata` inputs are accepted; responses use
    `metadata`.

    To enroll an opened container, supply `tare_weight`, `net_weight`, and optionally
    `full_net_weight`; gross becomes tare plus net. Do not supply both `weight` and
    `net_weight`, even if one is null. See the storage group for mass persistence rules.
    Prefer `POST /api/storage/items/update` for partial edits to existing records. Reserved
    bin barcodes and, for a non-admin, size or access-level changes to a bin member
    return `409`/`403`. A bin interaction holding the item does not
    block the edit; conflicting state is rejected when that interaction completes.
    """
    storage_service = StorageService(session)

    if await is_reserved_bin_barcode(item.barcode, session):
        raise HTTPException(status_code=409, detail="This barcode belongs to a bin.")
    existing = await storage_service.get_item(item.barcode)
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
        if (dimensions_changed or access_changed) and _is_admin(current_user) is False:
            raise api_error(
                403,
                "bin_item_edit_requires_admin",
                "Only an admin can change the size or access level of an item in a bin.",
            )
        if "dimensions" not in provided_fields:
            dimensions = existing.dimensions
        if "item_metadata" not in provided_fields:
            metadata = existing.item_metadata or {}
        if "access_level_id" not in provided_fields:
            access_level_id = existing.access_level_id
    try:
        db_item = await storage_service.create_item(
            barcode=item.barcode,
            name=item.name,
            dimensions=dimensions,
            weight=item.weight,
            preserve_existing_weight="weight" not in item.model_fields_set,
            metadata=metadata,
            access_level_id=access_level_id,
            mass_values={k: v for k, v in item.model_dump(exclude_unset=True).items() if k in (*MASS_FIELDS, "weight")},
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    locations = await storage_service.get_item_locations(item.barcode)

    return await _build_item_payload_with_bin(db_item, locations, current_user, session)


@router.post(
    "/items/update",
    dependencies=[Depends(require_relay_or_permissions("storage:write"))],
    summary="Update selected inventory fields",
    responses=response_docs(errors=(401, 403, 404, 409, 422)),
)
async def update_item(
    item: ItemUpdateRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> ItemWithLocations:
    """Update selected inventory fields.

    Takes the barcode and the fields to change in the request body. Omitted mass fields
    retain their values. `net_weight=null` clears gross and derived net; `tare_weight=null`
    or `full_net_weight=null` clears that reference. Legacy `weight=null` does not clear
    gross. Supplying both `weight` and `net_weight` returns `422`.
    Setting net requires a known tare, either stored or supplied.

    Changing tare alone preserves gross and recomputes net. Metadata replaces the object
    when provided. A size or access-level change to a bin member is admin-only
    (`403 bin_item_edit_requires_admin`) and may be applied while the bin is home or
    outside. Returns `404` for an absent barcode. These edits do not move
    hardware.
    """
    storage_service = StorageService(session)
    barcode = item.barcode

    existing = await storage_service.get_item(barcode)
    bin_service = BinStorageService(session)
    if existing is not None and await bin_service.item_bin(existing.id) is not None:
        dimensions_changed = item.dimensions is not None and item.dimensions != existing.dimensions
        access_changed = (
            "access_level_id" in item.model_fields_set
            and item.access_level_id != existing.access_level_id
        )
        if (dimensions_changed or access_changed) and _is_admin(current_user) is False:
            raise api_error(
                403,
                "bin_item_edit_requires_admin",
                "Only an admin can change the size or access level of an item in a bin.",
            )

    try:
        validate_mass(item.model_dump(exclude_unset=True), existing.tare_weight if existing else None)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    updated_item = await storage_service.update_item(
        barcode=barcode,
        name=item.name,
        dimensions=item.dimensions,
        weight=item.weight,
        metadata=item.item_metadata,
        access_level_id=item.access_level_id,
        mass_values={k: v for k, v in item.model_dump(exclude_unset=True).items() if k in (*MASS_FIELDS, "weight")},
    )

    if not updated_item:
        raise HTTPException(status_code=404, detail="Item not found")

    fresh_item = await storage_service.get_item(barcode)
    if fresh_item is None:
        fresh_item = updated_item

    locations = await storage_service.get_item_locations(barcode)

    return await _build_item_payload_with_bin(fresh_item, locations, current_user, session)


@router.post(
    "/items/delete",
    dependencies=[Depends(require_relay_or_permissions("storage:delete"))],
    summary="Delete an inventory record",
    responses=response_docs(model=MessageResponse, errors=(401, 403, 404, 409)),
)
@monitor_performance
async def delete_item(
    request: BarcodeRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_relay_or_current_user),
) -> Dict[str, str]:
    """Delete an inventory record.

    Takes the barcode in the request body. Deletes the item and its database location
    records; it does not physically retrieve it. An admin deleting a bin member also
    removes its bin membership; for everyone else the membership blocks the deletion.
    An unfinished bin interaction blocks deletion for everyone. Finished bin
    interactions keep the item's barcode and name in their record. Use this
    low-level endpoint only after reconciling physical occupancy. A missing record
    returns `404`.
    """
    storage_service = StorageService(session)
    barcode = request.barcode

    existing = await storage_service.get_item(barcode)
    bin_service = BinStorageService(session)
    in_bin = existing is not None and await bin_service.item_bin(existing.id) is not None
    if existing is not None and await bin_service.item_has_active_workflow(existing.id):
        raise api_error(
            409,
            "bin_workflow_conflict",
            "This item has a bin task in progress. Finish it before deleting the item.",
        )
    if in_bin and _is_admin(current_user) is False:
        raise api_error(
            403,
            "bin_item_delete_requires_admin",
            "Only an admin can delete an item that is in a bin.",
        )
    if existing is not None:
        if in_bin:
            await session.execute(
                delete(StorageBinItem).where(StorageBinItem.item_id == existing.id)
            )
            await session.flush()
        await bin_service.detach_item_history(existing)

    deleted = await storage_service.delete_item(barcode)

    if deleted is False:
        raise HTTPException(status_code=404, detail="Item not found")

    return {"message": "Item deleted successfully"}


# ---------------------------------------------------------------------------
# Item location endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/items/locations/add",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Assign an inventory location",
    responses=response_docs(errors=(400, 401, 403, 404, 409, 422)),
)
async def add_item_location(
    location: ItemLocationRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Assign an inventory location.

    Takes the barcode and the zero-based side/shelf/cell of the location in the request body.
    Adds database occupancy for an item without moving hardware. Occupied cells and
    incompatible bin state are
    rejected. Tray homes are usable while their tray is away; the tray's return then reports
    the occupied home instead. The status stream overlays these database locations on the
    grid.
    """
    storage_service = StorageService(session)
    barcode = location.barcode

    db_item = await storage_service.get_item(barcode)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    bin_service = BinStorageService(session)
    if await bin_service.item_bin(db_item.id) is not None:
        raise HTTPException(status_code=409, detail="This item is in a bin.")

    try:
        storage_location = await storage_service.add_item_location(
            barcode=barcode,
            side=location.side,
            shelf_idx=location.shelf_idx,
            cell_idx=location.cell_idx,
        )
    except CellOccupiedError as exc:
        logging.info("Item location rejected: %s", exc)
        raise api_error(409, "cell_unavailable", "") from exc
    except (ValidationError, ValueError) as exc:
        logging.info("Item location rejected: %s", exc)
        raise api_error(422, "invalid_location", "") from exc

    if not storage_location:
        raise HTTPException(status_code=400, detail="Failed to add location")

    return await _load_item_with_locations(barcode, storage_service, current_user)


async def _remove_item_location_impl(
    barcode: str,
    side: StorageModuleSideDto,
    shelf_idx: int,
    cell_idx: int,
    storage_service: StorageService,
) -> None:
    item = await storage_service.get_item(barcode)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    bin_service = BinStorageService(storage_service.session)
    if await bin_service.item_bin(item.id) is not None:
        raise HTTPException(status_code=409, detail="This item is in a bin.")
    success = await storage_service.remove_item_location(
        barcode=barcode,
        side=side,
        shelf_idx=shelf_idx,
        cell_idx=cell_idx,
    )

    if not success:
        raise HTTPException(status_code=404, detail="Location not found")


@router.post(
    "/items/locations/remove",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Remove an inventory location",
    responses=response_docs(errors=(401, 403, 404, 409)),
)
async def remove_item_location(
    request: ItemLocationRequest,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ItemWithLocations:
    """Remove an inventory location.

    Takes the barcode and the zero-based side/shelf/cell of the location in the request body.
    This does not move hardware or remove the storage-grid cell. Use only when physical
    occupancy has already been reconciled.
    """
    storage_service = StorageService(session)

    await _remove_item_location_impl(
        request.barcode, request.side, request.shelf_idx, request.cell_idx, storage_service
    )
    return await _load_item_with_locations(request.barcode, storage_service, current_user)


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
        raise _grid_load_error(exc) from exc

    return grid.model_dump()


@router.post(
    "/grid/shelf",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Add a shelf to the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 409, 500)),
)
async def add_grid_shelf(
    side: StorageModuleSideDto = Query(...)
) -> Dict[str, Any]:
    """Add a shelf to the robot grid.

    Adds one shelf at the next free index with a single cell, cloning geometry from the
    side's existing shelves where available, without moving hardware.
    """
    await ensure_laser_auto_calibration_not_running()
    try:
        grid = _get_robot_grid()
        grid.add_shelf(side)
        return {"success": True}
    except Exception as exc:
        raise _grid_update_error(exc) from exc


@router.delete(
    "/grid/shelf",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Delete a shelf from the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 409, 500)),
)
async def delete_shelf(
    side: StorageModuleSideDto = Query(...),
    shelf_idx: int = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Delete a shelf from the robot grid.

    Deletes the selected side/shelf and its cells from the persisted grid, without
    retrieving physical contents. This engineering operation changes storage geometry, not
    item inventory.
    """
    await ensure_laser_auto_calibration_not_running()

    # Removing a shelf reindexes the side, so it desyncs every shelf-index reference at or
    # above the deleted shelf. Refuse while a tray home or a stored item still names one.
    bin_ref = (
        await session.execute(
            select(StorageBin.id).where(
                StorageBin.side == side,
                StorageBin.active.is_(True),
                StorageBin.shelf_idx >= shelf_idx,
            )
        )
    ).first()
    item_ref = (
        await session.execute(
            select(ItemLocation.id).where(
                ItemLocation.side == side,
                ItemLocation.shelf_idx >= shelf_idx,
            )
        )
    ).first()
    if bin_ref is not None or item_ref is not None:
        raise HTTPException(
            status_code=409,
            detail="This shelf, or one above it, holds a bin or a stored item. Move or remove them first.",
        )

    try:
        grid = _get_robot_grid()
        grid.remove_shelf(side, shelf_idx)
        return {"success": True}
    except IndexError as exc:
        raise _grid_location_error(exc)
    except Exception as exc:
        raise _grid_update_error(exc) from exc


@router.post(
    "/grid/cell",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Add a cell to the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 409, 500)),
)
async def add_grid_cell(
    side: StorageModuleSideDto = Query(...), shelf_idx: int = Query(...)
) -> Dict[str, Any]:
    """Add a cell to the robot grid.

    Adds one cell at the next free index to the selected shelf, cloning geometry from
    existing cells where available, without moving hardware.
    """
    await ensure_laser_auto_calibration_not_running()
    try:
        grid = _get_robot_grid()
        grid.add_cell(side, shelf_idx)
        return {"success": True}
    except IndexError as exc:
        raise _grid_location_error(exc)
    except Exception as exc:
        raise _grid_update_error(exc) from exc


@router.delete(
    "/grid/cell",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Delete a cell from the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(401, 403, 404, 409, 500)),
)
async def delete_cell(
    side: StorageModuleSideDto = Query(...), shelf_idx: int = Query(...), cell_idx: int = Query(...),
    session: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Delete a cell from the robot grid.

    Deletes one side/shelf/cell from the persisted grid, without moving hardware. Reconcile
    item occupancy separately before changing storage geometry.
    """
    await ensure_laser_auto_calibration_not_running()

    # Deleting a cell desyncs the grid from any tray home or stored item that names it.
    bin_ref = (
        await session.execute(
            select(StorageBin.id).where(
                StorageBin.side == side,
                StorageBin.active.is_(True),
                StorageBin.shelf_idx == shelf_idx,
                StorageBin.cell_idx == cell_idx,
            )
        )
    ).first()
    item_ref = (
        await session.execute(
            select(ItemLocation.id).where(
                ItemLocation.side == side,
                ItemLocation.shelf_idx == shelf_idx,
                ItemLocation.cell_idx == cell_idx,
            )
        )
    ).first()
    if bin_ref is not None or item_ref is not None:
        raise HTTPException(
            status_code=409,
            detail="This cell holds a bin or a stored item. Move or remove it first.",
        )

    try:
        grid = _get_robot_grid()
        grid.remove_cell(side, shelf_idx, cell_idx)
        return {"success": True}
    except IndexError as exc:
        raise _grid_location_error(exc)
    except Exception as exc:
        raise _grid_update_error(exc) from exc


@router.put(
    "/grid/cell",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Update a cell in the robot grid",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 404, 409, 500)),
)
async def update_grid_cell(
    payload: GridCellUpdate,
    side: StorageModuleSideDto = Query(...), shelf_idx: int = Query(...), cell_idx: int = Query(...)
) -> Dict[str, Any]:
    """Update calibration point, status, and/or dimensions for one robot-grid cell.

    Omitted fields keep the cell's current values, so a partial payload updates only what
    it carries. x, y, and z_offset are applied together and create the cell's calibration
    point when it has none.
    """
    await ensure_laser_auto_calibration_not_running()
    try:
        grid = _get_robot_grid()
        cell = grid.get_cell(StorageModuleCellLocation(side=side, shelf_idx=shelf_idx, cell_idx=cell_idx))
    except IndexError as exc:
        raise _grid_location_error(exc)
    except Exception as exc:
        raise _grid_load_error(exc) from exc

    _validate_grid_cell_update(payload)
    _apply_grid_cell_update(cell, payload, side)

    grid.save()

    return {"success": True}


def _validate_grid_dimensions(dims: DimensionsPayload) -> None:
    for value in (dims.width, dims.height, dims.length):
        if not value > 0:
            raise api_error(400, "dimension_invalid", "Enter a size greater than zero.")


def _validate_grid_cell_update(payload: GridCellUpdate) -> None:
    if (
        payload.x is None
    ) != (payload.y is None) or (
        payload.x is None
    ) != (payload.z_offset is None):
        raise HTTPException(
            status_code=400,
            detail="Enter the X, Y and Z offset values together.",
        )

    if payload.status is not None:
        try:
            CellStatus(payload.status)
        except ValueError:
            raise api_error(400, "cell_status_invalid", "")

    if payload.dimensions is not None:
        _validate_grid_dimensions(payload.dimensions)


def _apply_grid_cell_update(cell: Cell, payload: GridCellUpdate, side: StorageModuleSide) -> None:
    if payload.status is not None:
        cell.status = CellStatus(payload.status)

    if payload.x is not None and payload.y is not None and payload.z_offset is not None:
        cell.calibration_point = CalibrationCoordinate(
            x=payload.x,
            y=payload.y,
            z_offset=payload.z_offset,
            s=side_to_s(side),
        )

    if payload.dimensions is not None:
        cell.dimensions = Dimensions(
            width=payload.dimensions.width,
            height=payload.dimensions.height,
            length=payload.dimensions.length,
        )

    if payload.is_manual is not None:
        cell.is_manual = payload.is_manual


@router.put(
    "/grid/cells",
    dependencies=[Depends(require_permissions("storage:write"))],
    summary="Update multiple cells in the robot grid (atomic)",
    responses=response_docs(model=BooleanSuccess, errors=(400, 401, 403, 404, 409, 500)),
)
async def update_grid_cells(
    payload: GridCellsBatchUpdate,
    side: StorageModuleSideDto = Query(...),
) -> Dict[str, Any]:
    """Atomically apply updates to multiple robot-grid cells.

    Every cell is resolved and every payload validated before anything is mutated, and the
    grid is saved once at the end. If any cell is missing or any payload is invalid, the
    whole request fails and nothing is changed.
    """
    await ensure_laser_auto_calibration_not_running()
    try:
        grid = _get_robot_grid()
    except Exception as exc:
        raise _grid_load_error(exc) from exc

    if len(payload.cells) == 0:
        raise HTTPException(status_code=400, detail="No cells to update")

    resolved: list[tuple[Cell, GridCellBatchItem]] = []

    for item in payload.cells:
        try:
            cell = grid.get_cell(
                StorageModuleCellLocation(side=side, shelf_idx=item.shelf_idx, cell_idx=item.cell_idx)
            )
        except IndexError as exc:
            raise _grid_location_error(exc)
        _validate_grid_cell_update(item)
        resolved.append((cell, item))

    for cell, item in resolved:
        _apply_grid_cell_update(cell, item, side)

    grid.save()

    return {"success": True}
