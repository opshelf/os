"""Inventory import jobs: created and cancelled by the relay, walked through on the kiosk."""

from __future__ import annotations

import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

import openshelf_os_server.core.robot as robot_core
from openshelf_os.config.settings import BARCODE_MAX_LENGTH
from openshelf_os_server.core.bin_storage import (
    BinStorageService,
    BinWorkflowConflict,
    ensure_worker_homed_for_bin_motion,
    ensure_worker_queue_empty_for_bin_start,
    is_io_box_occupied,
)
from openshelf_os_server.core.import_jobs import (
    MAX_UNITS,
    ImportJobError,
    ImportJobService,
    ImportRowInput,
    is_mock_mode,
    mutation_lock,
)
from openshelf_os_server.core.manual_motion import robot_command_enqueue_guard
from openshelf_os_server.core.mock_robot import get_mock_robot_runtime
from openshelf_os_server.database.models import ImportJob, User
from openshelf_os_server.database.session import get_db
from openshelf_os_server.documentation import response_docs
from openshelf_os_server.routers.auth_deps import (
    require_permissions,
    require_relay_or_admin,
    require_relay_or_permissions,
)
from openshelf_os_server.routers.storage import BinWorkflowResponse

router = APIRouter(tags=["import-jobs"])
logger = logging.getLogger(__name__)


class ImportRow(BaseModel):
    """One CSV row. `quantity` expands into that many units, each with its own label."""

    row_number: Optional[int] = Field(default=None, ge=1, description="Source CSV row, for reporting")
    barcode: Optional[str] = Field(
        default=None,
        max_length=BARCODE_MAX_LENGTH,
        description="Label barcode for the first unit; the device generates the rest, and all of them when omitted.",
    )
    name: str = Field(min_length=1, max_length=255)
    quantity: int = Field(default=1, ge=1, le=MAX_UNITS)
    small: bool = Field(default=False, description="Placed in a tray compartment instead of its own cell")
    expires_at: Optional[str] = Field(default=None, description="Expiry date, YYYY-MM-DD")
    access_level: Optional[str | int] = Field(default=None, description="Access level name or id")
    tare_weight: Optional[float] = Field(default=None, description="Container mass in grams")
    net_weight: Optional[float] = Field(default=None, description="Contents mass in grams; requires tare_weight")
    full_net_weight: Optional[float] = Field(default=None, description="Full contents reference in grams")
    width: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="Known item width in millimetres; the kiosk skips measuring it"
    )
    height: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="Known item height in millimetres; the kiosk skips measuring it"
    )
    length: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="Known item length in millimetres; the kiosk skips measuring it"
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Custom item metadata")


class ImportJobCreate(BaseModel):
    filename: Optional[str] = Field(default=None, max_length=255)
    rows: list[ImportRow] = Field(
        min_length=1,
        examples=[[{"row_number": 2, "barcode": "880000000500", "name": "Acetone 500 ml", "quantity": 2}]],
    )


class ImportCounts(BaseModel):
    total: int
    pending: int
    in_progress: int
    loaded: int
    skipped: int


class ImportUnitResponse(BaseModel):
    id: int
    position: int = Field(description="Zero-based load order")
    row_number: Optional[int]
    barcode: str
    name: str
    small: bool
    access_level_id: Optional[int]
    expires_at: Optional[str]
    status: Literal["pending", "in_progress", "loaded", "skipped"]
    stage: Optional[Literal["measured", "storing", "placed", "returning"]] = Field(
        description="Progress of an in-progress unit"
    )
    error: Optional[str] = Field(description="Last store or tray return failure")
    request_id: Optional[str] = Field(description="Use as the store_item request_id for a measured unit")
    planned_dimensions: Optional[dict[str, float]] = Field(
        default=None, description="Width, height and length in millimetres given by the import file (any subset)"
    )
    dimensions: Optional[dict[str, float]]
    item_id: Optional[int]
    bin_id: Optional[int]
    compartment: Optional[int]


class ImportStepResponse(BaseModel):
    kind: Literal["unit", "tray", "waiting"] = Field(
        description="unit: measure and store one item; tray: fill a tray with small items; waiting: the robot is still storing"
    )
    phase: Optional[Literal["start", "fetching", "filling", "returning", "return_failed", "failed"]] = None
    unit: Optional[ImportUnitResponse] = None
    units: Optional[list[ImportUnitResponse]] = None
    workflow: Optional[BinWorkflowResponse] = None
    error: Optional[str] = None


class ImportJobResponse(BaseModel):
    id: str
    state: Literal["active", "cancelling", "completed", "cancelled"]
    filename: Optional[str]
    cancel_source: Optional[Literal["relay", "kiosk"]]
    counts: ImportCounts
    created_at: str
    updated_at: str
    completed_at: Optional[str]
    skipped: list[ImportUnitResponse] = Field(description="Skipped units")
    in_progress: list[ImportUnitResponse]
    units: Optional[list[ImportUnitResponse]] = Field(default=None, description="Every unit, when requested")
    step: Optional[ImportStepResponse] = Field(default=None, description="The kiosk's next step")


class ImportJobEnvelope(BaseModel):
    job: Optional[ImportJobResponse]


class ImportStepResult(BaseModel):
    job: ImportJobResponse
    unit: Optional[ImportUnitResponse] = None
    notice: Optional[str] = Field(
        default=None, description="Why the machine skipped units during this call, for the operator"
    )


class UnitDimensions(BaseModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    length: float = Field(gt=0)


class BeginUnitRequest(BaseModel):
    dimensions: UnitDimensions


class PlaceInTrayRequest(BaseModel):
    unit_id: int
    height: float = Field(gt=0, description="Measured item height in millimeters")


def _http(exc: ImportJobError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail())


async def _load(service: ImportJobService, job_id: str) -> ImportJob:
    try:
        return await service.get(job_id)
    except ImportJobError as exc:
        raise _http(exc) from exc


async def _result(service: ImportJobService, job: ImportJob, unit: Any = None) -> dict[str, Any]:
    await service.reconcile(job)
    await service.publish(job)
    return {
        "job": await service.detail(job, include_step=True),
        "unit": service.serialize_unit(unit) if unit is not None else None,
        "notice": service.notice,
    }


@router.post(
    "",
    summary="Create an import job",
    responses=response_docs(model=ImportJobEnvelope, errors=(401, 403, 409, 422)),
)
async def create_import_job(
    body: ImportJobCreate,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(require_relay_or_admin),
) -> dict[str, Any]:
    """Create an import job.

    Expands each row into `quantity` units in row order and assigns label barcodes: the row
    barcode for the first unit, generated unique barcodes for the rest. The kiosk
    shows the job after the next PIN unlock. Rejects a second running job (`409
    import_active`), barcodes already on the device (`409 barcodes_exist`, listed in
    `detail.errors`), and invalid rows (`422 invalid_rows`, per row and field). At most
    5000 units per job. Nothing moves until an operator starts the first step.
    """
    rows = [ImportRowInput(**row.model_dump()) for row in body.rows]
    async with mutation_lock():
        service = ImportJobService(session)
        try:
            job = await service.create(rows, body.filename)
        except ImportJobError as exc:
            raise _http(exc) from exc
        await service.publish(job)
        return {"job": await service.detail(job)}


@router.get(
    "/active",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="Read the running import job",
    responses=response_docs(model=ImportJobEnvelope, errors=(401, 403)),
)
async def get_active_import_job(session: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Read the running import job.

    Returns the active or cancelling job with progress counts and the kiosk's next `step`,
    or `job: null`. Command results that arrived without an event are applied first, so a
    restarted kiosk resumes where it left off.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        job = await service.open_job()
        if job is None:
            return {"job": None}
        if await service.reconcile(job):
            await service.publish(job)
        return {"job": await service.detail(job, include_step=True)}


@router.get(
    "/{job_id}",
    dependencies=[Depends(require_relay_or_permissions("storage:read"))],
    summary="Read an import job",
    responses=response_docs(model=ImportJobEnvelope, errors=(401, 403, 404)),
)
async def get_import_job(
    job_id: str,
    units: bool = Query(False, description="Include every unit"),
    session: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Read an import job.

    Returns state, counts (loaded, skipped, in progress, pending), the skipped units, and
    the units in progress; `units=true` adds every unit. Finished jobs stay readable for their summary;
    `pending` units of a cancelled job were never loaded.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        job = await _load(service, job_id)
        if await service.reconcile(job):
            await service.publish(job)
        return {"job": await service.detail(job, include_units=units)}


@router.post(
    "/{job_id}/cancel",
    summary="Cancel an import job",
    responses=response_docs(model=ImportJobEnvelope, errors=(401, 403, 404)),
)
async def cancel_import_job(
    job_id: str,
    request: Request,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(require_relay_or_permissions("storage:write")),
) -> dict[str, Any]:
    """Cancel an import job.

    Never interrupts motion. The job becomes `cancelling` and offers no new units; loaded
    units stay. A tray out for the import is handed over as an ordinary outside tray: its
    placed units count as loaded and it is returned with the usual tray barcode scan. From
    the relay, a measured unit that was not yet submitted can still be stored or skipped;
    from the kiosk it is released. The job becomes `cancelled` once no store or tray motion
    is running. Repeated calls are harmless.
    """
    source = "relay" if getattr(request.state, "relay_service_authenticated", False) else "kiosk"
    async with mutation_lock():
        service = ImportJobService(session)
        job = await _load(service, job_id)
        await service.cancel(job, source)
        await service.publish(job)
        return {"job": await service.detail(job)}


@router.post(
    "/{job_id}/units/{unit_id}/begin",
    summary="Measure a unit and create its item",
    responses=response_docs(model=ImportStepResult, errors=(401, 403, 404, 409, 422)),
)
async def begin_import_unit(
    job_id: str,
    unit_id: int,
    body: BeginUnitRequest,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permissions("storage:write")),
) -> dict[str, Any]:
    """Measure a unit and create its item.

    Creates the inventory record with the measured dimensions so its label can be printed
    and scanned. Then submit `store_item` with the unit's `request_id`; the unit becomes
    `loaded` when that command succeeds. When no free cell fits, the unit is skipped
    instead and `notice` says why. Calling again before storing updates the dimensions. Units load in
    CSV order.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        job = await _load(service, job_id)
        try:
            unit = await service.begin_unit(job, unit_id, body.dimensions.model_dump())
        except ImportJobError as exc:
            raise _http(exc) from exc
        return await _result(service, job, unit)


@router.post(
    "/{job_id}/units/{unit_id}/skip",
    summary="Skip a unit",
    responses=response_docs(model=ImportStepResult, errors=(401, 403, 404, 409, 422)),
)
async def skip_import_unit(
    job_id: str,
    unit_id: int,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permissions("storage:write")),
) -> dict[str, Any]:
    """Skip a unit.

    The operator leaves it unloaded. The item created for a measured unit is deleted. A
    unit whose store was submitted, or that is already in a tray, cannot be skipped.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        job = await _load(service, job_id)
        try:
            unit = await service.skip_unit(job, unit_id)
        except ImportJobError as exc:
            raise _http(exc) from exc
        return await _result(service, job, unit)


@router.post(
    "/{job_id}/tray/start",
    summary="Fetch a tray for small units",
    responses=response_docs(model=ImportStepResult, errors=(401, 403, 404, 409, 503)),
)
async def start_import_tray(
    job_id: str,
    session: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_permissions("storage:write")),
) -> dict[str, Any]:
    """Fetch a tray for small units.

    Picks the home tray that can take the most of the next group of small units (same
    access level) and delivers it to the IO box. Requires an empty IO box and an idle,
    homed robot. When no tray could ever take the group, its units are skipped, `notice`
    says why, and no tray moves; a tray that is outside returns `409 trays_outside` instead.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        bins = BinStorageService(session)
        job = await _load(service, job_id)
        workflow = None
        try:
            if is_mock_mode():
                runtime = get_mock_robot_runtime()
                async with robot_command_enqueue_guard():
                    if await runtime.has_active_commands():
                        raise BinWorkflowConflict("Wait for the robot to finish storing")
                    workflow = await service.start_tray(job, current_user.id)
                    if workflow is not None:
                        await bins.transition(workflow, "awaiting_operator", "mock tray delivered")
                if workflow is not None:
                    runtime.set_bin_io_box_occupied(True)
                    await runtime.publish_status()
            else:
                worker = robot_core.robot_worker
                if worker is None:
                    raise HTTPException(status_code=503, detail="Robot worker is unavailable")
                async with robot_command_enqueue_guard():
                    if await is_io_box_occupied(worker):
                        raise BinWorkflowConflict("Remove the item from the IO box before fetching a tray")
                    ensure_worker_homed_for_bin_motion(worker)
                    await ensure_worker_queue_empty_for_bin_start(worker)
                    workflow = await service.start_tray(job, current_user.id)
                    if workflow is not None:
                        try:
                            await bins.start_retrieval(workflow, worker)
                        except Exception as exc:
                            await bins.fail(workflow, str(exc), tray_moved=False)
                            raise HTTPException(status_code=503, detail=f"Could not fetch the tray: {exc}") from exc
                if workflow is not None:
                    await bins.publish_workflow(worker, workflow)
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "robot_busy", "message": str(exc)}) from exc
        except ImportJobError as exc:
            raise _http(exc) from exc
        return await _result(service, job)


@router.post(
    "/{job_id}/tray/place",
    summary="Place a small unit in the tray",
    responses=response_docs(model=ImportStepResult, errors=(401, 403, 404, 409, 422)),
)
async def place_import_unit_in_tray(
    job_id: str,
    body: PlaceInTrayRequest,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permissions("storage:write")),
) -> dict[str, Any]:
    """Place a small unit in the tray.

    Creates the item with the tray's compartment footprint and the measured height and
    records it in the next free compartment, returned as `unit.compartment`. Print and
    scan its label before calling. A unit taller than the compartment is skipped and
    `notice` says why. A full tray returns `409 tray_full`; return it to continue with another tray.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        job = await _load(service, job_id)
        try:
            unit = await service.place_in_tray(job, body.unit_id, body.height)
        except ImportJobError as exc:
            raise _http(exc) from exc
        return await _result(service, job, unit)


@router.post(
    "/{job_id}/tray/return",
    summary="Return the import tray",
    responses=response_docs(model=ImportStepResult, errors=(401, 403, 404, 409, 503)),
)
async def return_import_tray(
    job_id: str,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permissions("storage:write")),
) -> dict[str, Any]:
    """Return the import tray.

    Ends filling and stores the tray with every item it holds. Requires the tray in the
    IO box and an idle, homed robot. Its units become `loaded` when the return succeeds;
    a failed return can be retried with this call. A stopped import's tray is returned
    with the usual tray barcode scan instead.
    """
    async with mutation_lock():
        service = ImportJobService(session)
        bins = BinStorageService(session)
        job = await _load(service, job_id)
        try:
            if is_mock_mode():
                runtime = get_mock_robot_runtime()
                async with robot_command_enqueue_guard():
                    if await runtime.has_active_commands():
                        raise BinWorkflowConflict("Wait for the robot to finish storing")
                    if not runtime.is_bin_io_box_occupied():
                        raise BinWorkflowConflict("Place the tray in the IO box before returning it")
                    returning = await service.begin_tray_return(job)
                    if returning is not None:
                        await bins.complete_return(returning)
                        await service.apply_tray_return(returning)
                runtime.set_bin_io_box_occupied(False)
                await runtime.publish_status()
            else:
                worker = robot_core.robot_worker
                if worker is None:
                    raise HTTPException(status_code=503, detail="Robot worker is unavailable")
                async with robot_command_enqueue_guard():
                    ensure_worker_homed_for_bin_motion(worker)
                    await ensure_worker_queue_empty_for_bin_start(worker)
                    if not await is_io_box_occupied(worker):
                        raise BinWorkflowConflict("Place the tray in the IO box before returning it")
                    returning = await service.begin_tray_return(job)
                    if returning is not None:
                        try:
                            await bins.enqueue_return(returning, worker)
                        except Exception as exc:
                            await service.fail_tray_return(returning, str(exc))
                            raise HTTPException(status_code=503, detail=f"Could not return the tray: {exc}") from exc
                if returning is not None:
                    await bins.publish_workflow(worker, returning)
        except BinWorkflowConflict as exc:
            raise HTTPException(status_code=409, detail={"code": "robot_busy", "message": str(exc)}) from exc
        except ImportJobError as exc:
            raise _http(exc) from exc
        return await _result(service, job)
