"""Relay-initiated inventory imports that the kiosk walks through one step at a time.

The device owns the job. The relay creates, reads, and cancels it; the kiosk loads the
active job after unlock and drives each step: measure, print, scan, and store a normal
unit through the IO box, or fetch one tray and place several small units in it.

A unit is `pending` until the kiosk begins it, `in_progress` while its item exists but is
not yet stored, then `loaded` once its store or tray return succeeds, or `skipped`. When
the machine skips a unit itself, the call that did it explains why in its `notice`.
In-progress units carry a stage: `measured` (item created, label not yet stored),
`storing` (its `store_item` command was admitted), `placed` (in a tray outside the system),
and `returning` (that tray's return is running).

Cancelling never interrupts motion. A cancelled job stops offering new units; a store or
tray motion already running finishes first, and units that never started stay `pending`.
A tray out for the import is handed over as an ordinary outside tray: its placed units
count as loaded, and the operator returns it with the usual tray barcode scan.
"""

from __future__ import annotations

import asyncio
import logging
import math
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional, Protocol, Sequence
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import openshelf_os.common as common
import openshelf_os_server.core.robot as robot_core
from openshelf_os.config.settings import BARCODE_MAX_LENGTH
from openshelf_os_server.core.bin_storage import (
    BATCH_OPERATION,
    BinStorageService,
    BinWorkflowConflict,
    reserved_bin_barcodes,
    reserved_bin_locations,
)
from openshelf_os_server.core.mass import validate_mass
from openshelf_os_server.core.storage import StorageService
from openshelf_os_server.core.validation import ValidationError
from openshelf_os_server.database.models import (
    AccessLevel,
    ImportJob,
    ImportJobUnit,
    Item,
    ItemLocation,
    RobotCommand,
    StorageBin,
    StorageBinItem,
    StorageBinWorkflow,
)
from openshelf_os_server.database.session import AsyncSessionLocal
from openshelf_os_server.settings import get_operating_mode

logger = logging.getLogger(__name__)

MAX_UNITS = 5000
# Barcodes per IN query, well under SQLite's bound-parameter limit.
BARCODE_QUERY_CHUNK = 500
OPEN_STATES = ("active", "cancelling")
TERMINAL_STATES = ("completed", "cancelled")
UNIT_STATUSES = ("pending", "in_progress", "loaded", "skipped")
CANCEL_SOURCES = ("relay", "kiosk")

# Every mutation reads and writes several rows; one device process serializes them.
_lock = asyncio.Lock()


class ImportJobError(Exception):
    """A rejected import request, mapped to an HTTP status by the router."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        errors: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.errors = errors or []

    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.errors:
            detail["errors"] = self.errors
        return detail


class EventPublisher(Protocol):
    async def publish_event(self, payload: dict[str, Any]) -> None: ...


@dataclass
class ImportRowInput:
    name: str
    quantity: int = 1
    row_number: Optional[int] = None
    barcode: Optional[str] = None
    small: bool = False
    expires_at: Optional[str] = None
    access_level: Optional[str | int] = None
    tare_weight: Optional[float] = None
    net_weight: Optional[float] = None
    full_net_weight: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    length: Optional[float] = None
    metadata: dict[str, Any] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _printable(value: str) -> bool:
    return all(0x20 <= ord(char) <= 0x7E for char in value)


def _upc_check_digit(body: str) -> str:
    odd = sum(int(digit) for digit in body[0::2])
    even = sum(int(digit) for digit in body[1::2])
    return str((10 - (odd * 3 + even) % 10) % 10)


def generate_barcode() -> str:
    """A 12-digit UPC-A in the 2xxxxx in-store range, the kiosk's own label format."""
    body = "2" + "".join(str(secrets.randbelow(10)) for _ in range(10))
    return body + _upc_check_digit(body)


def _normalize_date(value: Optional[str]) -> Optional[str]:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    try:
        return date.fromisoformat(cleaned[:10]).isoformat()
    except ValueError as exc:
        raise ValueError("expires_at must be a date (YYYY-MM-DD)") from exc


def _valid_dimensions(value: Any) -> Optional[dict[str, float]]:
    if not isinstance(value, dict):
        return None
    try:
        dimensions = {key: float(value[key]) for key in ("width", "height", "length")}
    except (KeyError, TypeError, ValueError):
        return None
    if any(isinstance(value[key], bool) for key in dimensions):
        return None
    if not all(math.isfinite(number) and number > 0 for number in dimensions.values()):
        return None
    return dimensions


def _robot_grid() -> Any:
    """The robot's storage grid, or the configured grid file when no robot runs."""
    worker = robot_core.robot_worker
    grid = getattr(getattr(worker, "_robot", None), "_grid", None)
    if grid is not None:
        return grid
    try:
        from openshelf_os.storage import StorageGrid  # noqa: PLC0415

        config = common.load_config_file(common.CONFIG_PATH)
        return StorageGrid.load_from_file(common.resolve_grid_path(config, common.CONFIG_PATH))
    except Exception as exc:  # pragma: no cover - depends on device files
        logger.warning("import_fit_check_grid_unavailable: %s", exc)
        return None


def is_mock_mode() -> bool:
    return get_operating_mode() == common.OperatingMode.mock


def event_publisher() -> Optional[EventPublisher]:
    if is_mock_mode():
        from openshelf_os_server.core import mock_robot  # noqa: PLC0415

        return mock_robot.mock_robot_runtime
    return robot_core.robot_worker


class ImportJobService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.bins = BinStorageService(session)
        # What the machine did on its own during this call (a skip, or a unit leaving the tray),
        # for the operator; never stored.
        self.notice: Optional[str] = None

    # ------------------------------------------------------------------ reads

    async def open_job(self) -> Optional[ImportJob]:
        return await self.session.scalar(
            select(ImportJob)
            .where(ImportJob.state.in_(OPEN_STATES))
            .order_by(ImportJob.created_at.desc())
            .limit(1)
        )

    async def get(self, job_id: str) -> ImportJob:
        job = await self.session.get(ImportJob, job_id)
        if job is None:
            raise ImportJobError(404, "not_found", "Import job not found")
        return job

    async def units(
        self, job: ImportJob, statuses: Optional[Iterable[str]] = None
    ) -> list[ImportJobUnit]:
        query = select(ImportJobUnit).where(ImportJobUnit.job_id == job.id)
        if statuses is not None:
            query = query.where(ImportJobUnit.status.in_(list(statuses)))
        return list((await self.session.scalars(query.order_by(ImportJobUnit.position))).all())

    async def unit(self, job: ImportJob, unit_id: int) -> ImportJobUnit:
        unit = await self.session.get(ImportJobUnit, unit_id)
        if unit is None or unit.job_id != job.id:
            raise ImportJobError(404, "not_found", "Import unit not found")
        return unit

    async def counts(self, job: ImportJob) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(ImportJobUnit.status, func.count())
                .where(ImportJobUnit.job_id == job.id)
                .group_by(ImportJobUnit.status)
            )
        ).all()
        counts = {status: 0 for status in UNIT_STATUSES}
        for status, count in rows:
            counts[str(status)] = int(count)
        counts["total"] = sum(counts[status] for status in UNIT_STATUSES)
        return counts

    # --------------------------------------------------------------- creation

    async def create(self, rows: Sequence[ImportRowInput], filename: Optional[str]) -> ImportJob:
        if await self.open_job() is not None:
            raise ImportJobError(409, "import_active", "Another import is still running on this machine")
        if not rows:
            raise ImportJobError(422, "invalid_rows", "Select at least one item to import")
        total = sum(max(0, row.quantity) for row in rows)
        if total > MAX_UNITS:
            raise ImportJobError(
                422, "too_many_units", f"An import can load at most {MAX_UNITS} units; this one has {total}"
            )

        levels = (await self.session.scalars(select(AccessLevel))).all()
        levels_by_id = {level.id: level for level in levels}
        levels_by_name = {level.name.strip().lower(): level for level in levels}
        errors: list[dict[str, Any]] = []
        labels: dict[str, int] = {}
        prepared: list[tuple[ImportRowInput, Optional[str], Optional[int], Optional[str], Optional[dict[str, float]]]] = []

        for index, row in enumerate(rows):
            number = row.row_number or index + 1

            def error(field_name: str, message: str) -> None:
                errors.append({"row": number, "field": field_name, "message": message})

            name = _clean(row.name)
            if name is None:
                error("name", "Name is required")
            elif len(name) > 255:
                error("name", "Name is longer than 255 characters")
            if row.quantity < 1:
                error("quantity", "Quantity must be at least 1")
            label = _clean(row.barcode)
            if label is not None:
                if len(label) > BARCODE_MAX_LENGTH or not _printable(label):
                    error("barcode", "Barcode must be printable ASCII up to 255 characters")
                elif label in labels:
                    error("barcode", f"Barcode {label} repeats row {labels[label]}")
                else:
                    labels[label] = number
            access_level_id: Optional[int] = None
            if row.access_level is not None and str(row.access_level).strip() != "":
                raw = str(row.access_level).strip()
                level = levels_by_name.get(raw.lower())
                if level is None and raw.isdigit():
                    level = levels_by_id.get(int(raw))
                if level is None and raw.lower().startswith("level ") is False and raw.isdigit():
                    level = levels_by_name.get(f"level {raw}")
                if level is None:
                    error("access_level", f"Unknown access level {raw}")
                else:
                    access_level_id = level.id
            expires_at: Optional[str] = None
            try:
                expires_at = _normalize_date(row.expires_at)
            except ValueError as exc:
                error("expires_at", str(exc))
            masses = {
                key: value
                for key, value in (
                    ("tare_weight", row.tare_weight),
                    ("net_weight", row.net_weight),
                    ("full_net_weight", row.full_net_weight),
                )
                if value is not None
            }
            try:
                validate_mass(masses)
            except ValidationError as exc:
                error("weight", str(exc))
            planned: dict[str, float] = {}
            for key in ("width", "height", "length"):
                value = getattr(row, key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                    error(key, f"{key.capitalize()} must be a positive number of millimetres")
                else:
                    planned[key] = float(value)
            prepared.append((row, label, access_level_id, expires_at, planned or None))

        if errors:
            raise ImportJobError(422, "invalid_rows", "Some rows cannot be imported", errors)

        existing = await self._existing_barcodes(labels)
        reserved = await reserved_bin_barcodes(self.session)
        conflicts = sorted((existing | reserved) & set(labels))
        if conflicts:
            raise ImportJobError(
                409,
                "barcodes_exist",
                "Some barcodes already exist on this machine",
                [{"row": labels[barcode], "field": "barcode", "barcode": barcode, "message": "Already on this machine"} for barcode in conflicts],
            )

        job = ImportJob(id=str(uuid4()), state="active", filename=_clean(filename))
        self.session.add(job)
        unlabelled = sum(row.quantity - (1 if label is not None else 0) for row, label, _, _, _ in prepared)
        generated = iter(await self._new_barcodes(unlabelled, set(labels) | reserved))
        position = 0
        for row, label, access_level_id, expires_at, planned_dimensions in prepared:
            name = _clean(row.name) or ""
            metadata = {str(key): value for key, value in (row.metadata or {}).items() if str(key).strip()}
            for copy in range(row.quantity):
                barcode = label if copy == 0 and label is not None else next(generated)
                self.session.add(
                    ImportJobUnit(
                        job_id=job.id,
                        position=position,
                        row_number=row.row_number,
                        barcode=barcode,
                        name=name,
                        small=bool(row.small),
                        access_level_id=access_level_id,
                        expires_at=expires_at,
                        tare_weight=row.tare_weight,
                        net_weight=row.net_weight,
                        full_net_weight=row.full_net_weight,
                        planned_dimensions=dict(planned_dimensions) if planned_dimensions else None,
                        unit_metadata=metadata,
                        status="pending",
                    )
                )
                position += 1
        await self.session.commit()
        return job

    async def _existing_barcodes(self, barcodes: Iterable[str]) -> set[str]:
        """Which of `barcodes` items already use, in a few chunked queries."""
        values = list(barcodes)
        found: set[str] = set()
        for start in range(0, len(values), BARCODE_QUERY_CHUNK):
            chunk = values[start : start + BARCODE_QUERY_CHUNK]
            found.update((await self.session.scalars(select(Item.barcode).where(Item.barcode.in_(chunk)))).all())
        return found

    async def _new_barcodes(self, count: int, taken: set[str]) -> list[str]:
        """`count` generated barcodes that no item, tray, or `taken` barcode uses.

        Candidates are checked in batches; only the rare collisions are drawn again.
        """
        fresh: list[str] = []
        chosen: set[str] = set()
        for _ in range(100):
            missing = count - len(fresh)
            if missing <= 0:
                return fresh
            candidates = {generate_barcode() for _ in range(missing)} - taken - chosen
            unused = candidates - await self._existing_barcodes(candidates)
            fresh.extend(unused)
            chosen.update(unused)
        if len(fresh) >= count:
            return fresh[:count]
        raise ImportJobError(503, "barcode_generation_failed", "Could not generate unique barcodes")

    # ---------------------------------------------------------- reconciliation

    async def reconcile(self, job: ImportJob) -> bool:
        """Catch up units with command and tray results that arrived without an event."""
        changed = False
        for unit in await self.units(job, ["in_progress"]):
            if unit.request_id is None:
                continue
            if unit.stage in ("measured", "storing"):
                command = await self.session.scalar(
                    select(RobotCommand)
                    .where(RobotCommand.request_id == unit.request_id)
                    .order_by(RobotCommand.created_at.desc())
                    .limit(1)
                )
                if command is None:
                    continue
                if command.status in ("queued", "running"):
                    if unit.stage != "storing":
                        unit.stage = "storing"
                        changed = True
                elif command.status == "succeeded":
                    self._mark_loaded(unit)
                    changed = True
                elif command.status == "failed":
                    self._mark_store_failed(job, unit, (command.result or {}).get("error"))
                    changed = True
            elif unit.stage == "returning":
                workflow = await self.session.get(StorageBinWorkflow, unit.request_id)
                if workflow is None:
                    continue
                if workflow.state == "completed":
                    self._mark_loaded(unit)
                    changed = True
                elif workflow.state in ("failed", "cancelled"):
                    self._mark_return_failed(unit, workflow.error)
                    changed = True
        if changed:
            await self.session.commit()
        if await self.settle(job):
            changed = True
        return changed

    def _mark_loaded(self, unit: ImportJobUnit) -> None:
        unit.status = "loaded"
        unit.stage = None
        unit.error = None

    def _mark_store_failed(self, job: ImportJob, unit: ImportJobUnit, error: Any) -> None:
        unit.stage = "measured"
        unit.error = str(error or "The robot could not store this item")
        unit.attempts += 1
        unit.request_id = self._request_id(job, unit)

    def _mark_return_failed(self, unit: ImportJobUnit, error: Any) -> None:
        unit.stage = "placed"
        unit.request_id = None
        unit.error = f"Bin return failed: {error or 'unknown error'}"

    @staticmethod
    def _request_id(job: ImportJob, unit: ImportJobUnit) -> str:
        return f"import-{job.id}-{unit.position}-{unit.attempts}"

    async def settle(self, job: ImportJob) -> bool:
        """Finish a job whose remaining work is done, or whose cancellation has drained."""
        if job.state not in OPEN_STATES:
            return False
        if job.state == "cancelling":
            await self._release_tray(job)
        counts = await self.counts(job)
        tray_busy = await self._tray_workflow_active(job)
        if job.state == "active" and counts["pending"] == 0 and counts["in_progress"] == 0 and not tray_busy:
            job.state = "completed"
        elif job.state == "cancelling" and counts["in_progress"] == 0 and not tray_busy:
            job.state = "cancelled"
        else:
            return False
        job.completed_at = _utc_now()
        job.tray_workflow_id = None
        await self.session.commit()
        return True

    async def _release_tray(self, job: ImportJob) -> None:
        """Hand a stopped import's tray to the standard return procedure.

        Its placed items are already recorded in the tray, so they count as loaded; the
        operator returns the tray by scanning its barcode. A tray still being delivered is
        released once it arrives, and a return already moving finishes on its own.
        """
        workflow = await self._tray_workflow(job)
        if workflow is not None and workflow.state == "retrieving_bin":
            return
        changed = False
        if workflow is not None and workflow.state == "awaiting_operator":
            await self.bins.close_batch(workflow)
            changed = True
        for unit in await self.units(job, ["in_progress"]):
            if unit.stage == "placed":
                self._mark_loaded(unit)
                changed = True
        if changed:
            await self.session.commit()

    async def _tray_workflow(self, job: ImportJob) -> Optional[StorageBinWorkflow]:
        if job.tray_workflow_id is None:
            return None
        return await self.session.get(StorageBinWorkflow, job.tray_workflow_id)

    async def _tray_workflow_active(self, job: ImportJob) -> bool:
        workflow = await self._tray_workflow(job)
        return workflow is not None and workflow.state in ("retrieving_bin", "awaiting_operator")

    # ------------------------------------------------------------ next step

    async def _trays(self) -> list[StorageBin]:
        return list((await self.session.scalars(select(StorageBin).where(StorageBin.active.is_(True)))).all())

    def _tray_takes(self, trays: Sequence[StorageBin], unit: ImportJobUnit) -> bool:
        """Whether some tray's compartment takes the unit by its known size, free or not."""
        return any(self.bins.batch_fits(tray, unit.planned_dimensions) for tray in trays)

    @staticmethod
    def _misfit_reason(unit: ImportJobUnit, trays: Sequence[StorageBin]) -> str:
        if not trays:
            return f"{unit.name} is marked for a multi-item bin, but this machine has none, so it is stored on its own."
        size = "×".join(
            f"{unit.planned_dimensions[key]:g}"
            for key in ("width", "length", "height")
            if key in (unit.planned_dimensions or {})
        )
        return (
            f"{unit.name} ({size} mm) does not fit any multi-item bin compartment, so it is stored on its own."
        )

    async def _no_tray(self) -> ImportJobError:
        """Why no home tray can be fetched for the next small items."""
        outside = await self.session.scalar(
            select(StorageBin.id).where(StorageBin.active.is_(True), StorageBin.status != "at_home").limit(1)
        )
        if outside is not None:
            # A tray that is out may be the one these items need; the operator returns it first.
            return ImportJobError(
                409, "trays_outside", "A bin is outside the machine; return it, then fetch a bin again"
            )
        return ImportJobError(
            409, "no_tray_free", "No multi-item bin with a free compartment fits this item; skip it to continue"
        )

    async def _small_group(
        self, job: ImportJob, first: ImportJobUnit, trays: Sequence[StorageBin]
    ) -> list[ImportJobUnit]:
        """The pending small units of the first one's access level that some tray takes."""
        return [
            unit
            for unit in await self.units(job, ["pending"])
            if unit.small and unit.access_level_id == first.access_level_id and self._tray_takes(trays, unit)
        ]

    async def _plan(
        self, group: Sequence[ImportJobUnit], outside: Optional[StorageBin] = None, placed: int = 0
    ) -> dict[int, StorageBin]:
        """Which tray each unit of the group goes to, by unit id; `outside` is a tray out for filling
        that already holds `placed` units of this trip."""
        if not group:
            return {}
        trays = await self.bins.batch_trays(group[0].access_level_id, None if outside is None else outside.id)
        return self.bins.plan_batch(
            [(row, capacity) for row, _, capacity in trays],
            [(unit.id, unit.planned_dimensions) for unit in group],
            None if outside is None else outside.id,
            placed,
        )

    async def _batch_tray(
        self, group: Sequence[ImportJobUnit]
    ) -> tuple[Optional[StorageBin], list[ImportJobUnit]]:
        """The tray to fetch for the group's first unit, and the units the plan puts in it."""
        plan = await self._plan(group)
        tray = plan.get(group[0].id)
        if tray is None:
            return None, list(group)
        return tray, [unit for unit in group if (planned := plan.get(unit.id)) is not None and planned.id == tray.id]

    async def next_step(self, job: ImportJob) -> Optional[dict[str, Any]]:
        """What the kiosk should show next, or None when it has nothing left to do."""
        if job.state not in OPEN_STATES:
            return None
        in_progress = await self.units(job, ["in_progress"])
        tray_step = await self._tray_step(job, in_progress)
        if tray_step is not None:
            return tray_step
        measured = next((unit for unit in in_progress if unit.stage == "measured"), None)
        if measured is not None:
            return {"kind": "unit", "unit": self.serialize_unit(measured)}
        if job.state == "active":
            first = await self._current_pending(job)
            if first is not None:
                if first.small:
                    trays = await self._trays()
                    if not self._tray_takes(trays, first):
                        # Too large for every compartment: it is stored on its own.
                        return {"kind": "unit", "unit": self.serialize_unit(first), "reason": self._misfit_reason(first, trays)}
                    tray, offered = await self._batch_tray(await self._small_group(job, first, trays))
                    # Show the items the plan puts in the tray to be fetched; the rest wait for another.
                    return {
                        "kind": "tray",
                        "phase": "start",
                        "workflow": None,
                        "units": [self.serialize_unit(unit) for unit in offered],
                        "error": None,
                        "reason": None if tray is not None else (await self._no_tray()).message,
                    }
                return {"kind": "unit", "unit": self.serialize_unit(first)}
        storing = [unit for unit in in_progress if unit.stage == "storing"]
        if storing:
            return {"kind": "waiting", "units": [self.serialize_unit(unit) for unit in storing]}
        return None

    async def _tray_step(
        self, job: ImportJob, in_progress: Sequence[ImportJobUnit]
    ) -> Optional[dict[str, Any]]:
        workflow = await self._tray_workflow(job)
        placed = [unit for unit in in_progress if unit.stage == "placed"]
        returning = [unit for unit in in_progress if unit.stage == "returning"]
        if workflow is not None and workflow.state in ("retrieving_bin", "awaiting_operator", "failed"):
            phase = {
                "retrieving_bin": "fetching",
                "awaiting_operator": "filling",
                "failed": "failed",
            }[workflow.state]
            members = [unit for unit in placed if unit.bin_id == workflow.bin_id]
            group: list[ImportJobUnit] = []
            if job.state == "active":
                anchor = members[0] if members else None
                pending = await self.units(job, ["pending"])
                small = [unit for unit in pending if unit.small]
                if phase == "failed":
                    # Try again fetches for the next item, so the step stays only while a tray takes it.
                    trays = await self._trays()
                    head = pending[0] if pending else None
                    small = (
                        await self._small_group(job, head, trays)
                        if head is not None and head.small and self._tray_takes(trays, head)
                        else []
                    )
                if anchor is None and small:
                    anchor = small[0]
                if anchor is not None:
                    group = [unit for unit in small if unit.access_level_id == anchor.access_level_id]
                    if phase != "failed":
                        # Offer only the items the plan puts in the tray being delivered; the rest wait.
                        tray_row = await self.session.get(StorageBin, workflow.bin_id)
                        plan = await self._plan(group, tray_row, len(members)) if tray_row is not None else {}
                        group = [
                            unit for unit in group if (planned := plan.get(unit.id)) is not None and planned.id == workflow.bin_id
                        ]
            # A failed delivery only matters while its items still wait for a tray.
            if phase != "failed" or group:
                return {
                    "kind": "tray",
                    "phase": phase,
                    "workflow": await self.bins.serialize_workflow(workflow),
                    "units": [self.serialize_unit(unit) for unit in [*members, *group]],
                    "error": workflow.error,
                }
        if returning or (
            # An empty tray going back has no returning units; its return workflow tracks it.
            workflow is not None
            and workflow.state == "completed"
            and await self.session.scalar(
                select(StorageBinWorkflow.id)
                .where(
                    StorageBinWorkflow.bin_id == workflow.bin_id,
                    StorageBinWorkflow.operation == "return",
                    StorageBinWorkflow.state == "returning_bin",
                )
                .limit(1)
            )
            is not None
        ):
            return {
                "kind": "tray",
                "phase": "returning",
                "workflow": None,
                "units": [self.serialize_unit(unit) for unit in returning],
                "error": None,
            }
        if placed:
            return {
                "kind": "tray",
                "phase": "return_failed",
                "workflow": None,
                "units": [self.serialize_unit(unit) for unit in placed],
                "error": next((unit.error for unit in placed if unit.error), None),
            }
        return None

    # ------------------------------------------------------------- serialize

    @staticmethod
    def serialize_unit(unit: ImportJobUnit) -> dict[str, Any]:
        return {
            "id": unit.id,
            "position": unit.position,
            "row_number": unit.row_number,
            "barcode": unit.barcode,
            "name": unit.name,
            "small": unit.small,
            "access_level_id": unit.access_level_id,
            "expires_at": unit.expires_at,
            "status": unit.status,
            "stage": unit.stage,
            "error": unit.error,
            "request_id": unit.request_id,
            "planned_dimensions": unit.planned_dimensions,
            "dimensions": unit.dimensions,
            "item_id": unit.item_id,
            "bin_id": unit.bin_id,
            "compartment": unit.compartment,
        }

    async def summary(self, job: ImportJob) -> dict[str, Any]:
        return {
            "id": job.id,
            "state": job.state,
            "filename": job.filename,
            "cancel_source": job.cancel_source,
            "counts": await self.counts(job),
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }

    async def detail(
        self, job: ImportJob, *, include_units: bool = False, include_step: bool = False
    ) -> dict[str, Any]:
        payload = await self.summary(job)
        # Progress polls read this often; only load every unit when it was asked for.
        units = await self.units(job) if include_units else await self.units(job, ["skipped", "in_progress"])
        payload["skipped"] = [self.serialize_unit(unit) for unit in units if unit.status == "skipped"]
        payload["in_progress"] = [
            self.serialize_unit(unit) for unit in units if unit.status == "in_progress"
        ]
        if include_units:
            payload["units"] = [self.serialize_unit(unit) for unit in units]
        if include_step:
            payload["step"] = await self.next_step(job)
        return payload

    # ------------------------------------------------------------ unit steps

    def _require_active(self, job: ImportJob) -> None:
        if job.state != "active":
            raise ImportJobError(409, "import_not_active", "This import is no longer running")

    async def _current_pending(self, job: ImportJob) -> Optional[ImportJobUnit]:
        return await self.session.scalar(
            select(ImportJobUnit)
            .where(ImportJobUnit.job_id == job.id, ImportJobUnit.status == "pending")
            .order_by(ImportJobUnit.position)
            .limit(1)
        )

    async def free_cell_fits(self, dimensions: dict[str, float]) -> bool:
        """Whether some free cell fits `dimensions` beyond the stores already queued."""
        occupied = {
            (row.side, row.shelf_idx, row.cell_idx)
            for row in (
                await self.session.execute(
                    select(ItemLocation.side, ItemLocation.shelf_idx, ItemLocation.cell_idx)
                )
            ).all()
        }
        excluded = occupied | await reserved_bin_locations(self.session)
        queued_stores = sum(
            1
            for payload in (
                await self.session.scalars(
                    select(RobotCommand.payload).where(RobotCommand.status.in_(("queued", "running")))
                )
            ).all()
            if isinstance(payload, dict) and payload.get("command") == "store_item"
        )
        if is_mock_mode():
            from openshelf_os_server.core.mock_robot import (  # noqa: PLC0415
                MOCK_CELLS_PER_SHELF,
                MOCK_SHELVES_PER_SIDE,
                MOCK_SIDES,
                mock_cell_fits,
            )

            if not mock_cell_fits(dimensions):
                return False
            cells = len(MOCK_SIDES) * MOCK_SHELVES_PER_SIDE * MOCK_CELLS_PER_SHELF
            return cells - len(excluded) - queued_stores > 0
        grid = _robot_grid()
        if grid is None:
            # Without a grid the robot's own allocation decides; never skip on a guess.
            return True
        from openshelf_os.storage import CellItem, CellStatus, Dimensions  # noqa: PLC0415

        candidate = CellItem(dimensions=Dimensions(**dimensions))
        fitting = 0
        for side, shelves in (("front_module", grid.front_module), ("back_module", grid.back_module)):
            for shelf in shelves:
                for cell in shelf.cells:
                    if (side, shelf.shelf_idx, cell.cell_idx) in excluded:
                        continue
                    if cell.status == CellStatus.AVAILABLE and grid.cell_fits_item(cell, candidate):
                        fitting += 1
        return fitting > queued_stores

    def _item_metadata(self, job: ImportJob, unit: ImportJobUnit) -> dict[str, Any]:
        metadata: dict[str, Any] = dict(unit.unit_metadata or {})
        metadata["name"] = unit.name
        if unit.expires_at:
            metadata["expires_at"] = unit.expires_at
        metadata["import_job_id"] = job.id
        return metadata

    async def _create_item(
        self, job: ImportJob, unit: ImportJobUnit, dimensions: dict[str, float]
    ) -> Item:
        masses = {
            key: value
            for key, value in (
                ("tare_weight", unit.tare_weight),
                ("net_weight", unit.net_weight),
                ("full_net_weight", unit.full_net_weight),
            )
            if value is not None
        }
        return await StorageService(self.session).create_item(
            barcode=unit.barcode,
            name=unit.name,
            dimensions=dimensions,
            metadata=self._item_metadata(job, unit),
            access_level_id=unit.access_level_id,
            mass_values=masses,
        )

    async def _barcode_taken(self, unit: ImportJobUnit) -> bool:
        item_id = await self.session.scalar(select(Item.id).where(Item.barcode == unit.barcode))
        if item_id is not None and item_id != unit.item_id:
            return True
        return unit.barcode in await reserved_bin_barcodes(self.session)

    async def _skip(self, unit: ImportJobUnit) -> None:
        await self._discard_item(unit)
        unit.status = "skipped"
        unit.stage = None
        unit.request_id = None
        unit.bin_id = None
        unit.compartment = None

    async def _discard_item(self, unit: ImportJobUnit) -> None:
        """Delete the item an unstored unit created, so a skipped unit leaves no record."""
        if unit.item_id is None:
            return
        item = await self.session.get(Item, unit.item_id)
        unit.item_id = None
        if item is None:
            return
        located = await self.session.scalar(
            select(ItemLocation.id).where(ItemLocation.item_id == item.id).limit(1)
        )
        binned = await self.session.scalar(
            select(StorageBinItem.id).where(StorageBinItem.item_id == item.id).limit(1)
        )
        if located is not None or binned is not None:
            logger.warning("import_unit_item_kept item=%s: it is already stored", item.barcode)
            return
        await self.session.delete(item)

    async def begin_unit(
        self, job: ImportJob, unit_id: int, dimensions: dict[str, Any]
    ) -> ImportJobUnit:
        """Create the item for a normal unit, or auto-skip it when its barcode is taken."""
        self._require_active(job)
        unit = await self.unit(job, unit_id)
        measured = _valid_dimensions(dimensions)
        if measured is None:
            raise ImportJobError(422, "invalid_dimensions", "Width, height and length must be positive numbers")
        if unit.small:
            # Only a small unit no tray compartment takes is stored on its own.
            await self.bins.sync_config()
            if self._tray_takes(await self._trays(), unit):
                raise ImportJobError(409, "small_unit", "This item goes in a multi-item bin")
        if unit.status == "in_progress" and unit.stage == "measured":
            if await self._command_admitted(unit):
                raise ImportJobError(409, "unit_storing", "This item is already being stored")
        elif unit.status == "pending":
            current = await self._current_pending(job)
            if current is None or current.id != unit.id:
                raise ImportJobError(409, "not_current", "Load the items in CSV order")
        else:
            raise ImportJobError(409, "unit_not_pending", "This item was already handled")

        if await self._barcode_taken(unit):
            await self._skip(unit)
            self.notice = f"{unit.name} was skipped: its barcode already exists on this machine."
        elif not await self.free_cell_fits(measured):
            # The operator chooses another size or skips the item.
            raise ImportJobError(409, "no_cell_fits", "No free storage cell fits this item")
        else:
            item = await self._create_item(job, unit, measured)
            if unit.status == "pending":
                unit.attempts += 1
            unit.status = "in_progress"
            unit.stage = "measured"
            unit.error = None
            unit.item_id = item.id
            unit.dimensions = measured
            unit.request_id = self._request_id(job, unit)
            workflow = await self._tray_workflow(job)
            if workflow is not None and workflow.state == "failed":
                # The operator moved on from the failed delivery; a moved bin is recovered by the
                # existing failed/interrupted bin task handling.
                job.tray_workflow_id = None
        await self.session.commit()
        await self.settle(job)
        return unit

    async def _command_admitted(self, unit: ImportJobUnit) -> bool:
        if unit.request_id is None:
            return False
        status = await self.session.scalar(
            select(RobotCommand.status).where(RobotCommand.request_id == unit.request_id).limit(1)
        )
        return status in ("queued", "running", "succeeded")

    async def skip_unit(self, job: ImportJob, unit_id: int) -> ImportJobUnit:
        unit = await self.unit(job, unit_id)
        if unit.status == "pending":
            if job.state != "active":
                raise ImportJobError(409, "import_not_active", "This import is no longer running")
        elif unit.status == "in_progress" and unit.stage == "measured":
            await self.reconcile(job)
            await self.session.refresh(unit)
            if unit.stage != "measured" or await self._command_admitted(unit):
                raise ImportJobError(409, "unit_storing", "This item is already being stored")
        else:
            raise ImportJobError(409, "unit_not_skippable", "This item can no longer be skipped")
        await self._skip(unit)
        await self.session.commit()
        await self.settle(job)
        return unit

    async def cancel(self, job: ImportJob, source: str) -> ImportJob:
        if source not in CANCEL_SOURCES:
            raise ValueError("unknown cancel source")
        if job.state in TERMINAL_STATES or job.state == "cancelling":
            return job
        await self.reconcile(job)
        if job.state != "active":
            return job
        job.state = "cancelling"
        job.cancel_source = source
        if source == "kiosk":
            # The operator stopped here: a measured item that never reached the robot goes
            # back to pending with its record removed.
            for unit in await self.units(job, ["in_progress"]):
                if unit.stage == "measured" and not await self._command_admitted(unit):
                    await self._discard_item(unit)
                    unit.status = "pending"
                    unit.stage = None
                    unit.request_id = None
                    unit.error = None
                    unit.dimensions = None
        await self.session.commit()
        await self.settle(job)
        return job

    # ------------------------------------------------------------- tray steps

    async def start_tray(self, job: ImportJob, user_id: Optional[int]) -> StorageBinWorkflow:
        """Claim a tray for the next group of small units.

        Returns the batch workflow in `retrieving_bin`; the caller starts its delivery. When no
        home tray with a free compartment takes the first unit, nothing moves and the refusal
        says why; the operator skips the unit to continue.
        """
        self._require_active(job)
        workflow = await self._tray_workflow(job)
        if workflow is not None and workflow.state in ("retrieving_bin", "awaiting_operator"):
            raise ImportJobError(409, "tray_active", "A bin is already out for this import")
        if any(unit.stage in ("placed", "returning") for unit in await self.units(job, ["in_progress"])):
            raise ImportJobError(409, "tray_outside", "Return the current bin first")
        first = await self._current_pending(job)
        await self.bins.sync_config()
        trays = await self._trays()
        if first is None or not first.small or not self._tray_takes(trays, first):
            raise ImportJobError(409, "not_current", "The next item does not go in a multi-item bin")
        tray, _ = await self._batch_tray(await self._small_group(job, first, trays))
        if tray is None:
            raise await self._no_tray()
        try:
            workflow = await self.bins.begin_batch(tray, user_id, job.id)
        except BinWorkflowConflict as exc:
            raise ImportJobError(409, "tray_unavailable", str(exc)) from exc
        job.tray_workflow_id = workflow.id
        await self.session.commit()
        return workflow

    async def place_in_tray(self, job: ImportJob, unit_id: int, height: Any) -> ImportJobUnit:
        """Create a small unit's item and put it in the batch tray's next free compartment."""
        workflow = await self._tray_workflow(job)
        if workflow is None or workflow.state != "awaiting_operator":
            raise ImportJobError(409, "tray_not_ready", "Wait for the bin to arrive.")
        if job.state != "active":
            raise ImportJobError(409, "import_not_active", "This import was stopped")
        unit = await self.unit(job, unit_id)
        if unit.status != "pending" or not unit.small:
            raise ImportJobError(409, "unit_not_pending", "This item was already handled")
        try:
            measured_height = float(height)
        except (TypeError, ValueError):
            measured_height = math.nan
        if isinstance(height, bool) or not math.isfinite(measured_height) or measured_height <= 0:
            raise ImportJobError(422, "invalid_dimensions", "Height must be a positive number")
        tray = await self.session.get(StorageBin, workflow.bin_id)
        if tray is None:
            raise ImportJobError(409, "tray_missing", "This bin no longer exists.")
        members = (
            await self.session.scalars(
                select(Item.access_level_id)
                .join(StorageBinItem, StorageBinItem.item_id == Item.id)
                .where(StorageBinItem.bin_id == tray.id)
            )
        ).all()
        if any(level != unit.access_level_id for level in members):
            raise ImportJobError(409, "access_level_mismatch", "This item belongs in a bin of another access level")
        if await self._barcode_taken(unit):
            await self._skip(unit)
            self.notice = f"{unit.name} was skipped: its barcode already exists on this machine."
            await self.session.commit()
            await self.settle(job)
            return unit
        footprint = {key: value for key, value in (unit.planned_dimensions or {}).items() if key != "height"}
        if not self.bins.batch_fits(tray, footprint):
            raise ImportJobError(409, "unit_wrong_tray", "This item does not fit this bin's compartments")
        maximum = self.bins.batch_max_height(tray)
        if measured_height > maximum:
            # Not skipped: the measured height leaves this bin for a taller one, else to storing it alone.
            unit.planned_dimensions = {**(unit.planned_dimensions or {}), "height": measured_height}
            later = (
                "It waits for a taller bin."
                if self._tray_takes(await self._trays(), unit)
                else "It is stored on its own after this bin."
            )
            self.notice = f"{unit.name} is taller than this bin's compartments (at most {maximum:g} mm). {later}"
            await self.session.commit()
            return unit
        dimensions = self.bins.batch_item_dimensions(tray, measured_height, unit.planned_dimensions)
        probe = Item(barcode=unit.barcode, name=unit.name, dimensions=dimensions, access_level_id=unit.access_level_id)
        error = await self.bins.batch_place_error(workflow, probe)
        if error == "This bin is full.":
            raise ImportJobError(409, "tray_full", "This bin is full; return it to continue")
        if error:
            raise ImportJobError(409, "tray_rejected", error)
        item = await self._create_item(job, unit, dimensions)
        try:
            compartment = await self.bins.place_in_batch(workflow, item)
        except BinWorkflowConflict as exc:
            await self.session.rollback()
            await self._discard_item_by_id(item.id)
            raise ImportJobError(409, "tray_rejected", str(exc)) from exc
        unit.status = "in_progress"
        unit.stage = "placed"
        unit.item_id = item.id
        unit.bin_id = tray.id
        unit.compartment = compartment
        unit.dimensions = dimensions
        unit.error = None
        unit.attempts += 1
        await self.session.commit()
        return unit

    async def _discard_item_by_id(self, item_id: int) -> None:
        item = await self.session.get(Item, item_id)
        if item is not None:
            await self.session.delete(item)
            await self.session.commit()

    async def tray_for_return(self, job: ImportJob) -> StorageBin:
        """The tray the kiosk returns next: the filling batch tray or one whose return failed."""
        workflow = await self._tray_workflow(job)
        if workflow is not None and workflow.state == "retrieving_bin":
            raise ImportJobError(409, "tray_not_ready", "Wait for the bin to arrive.")
        bin_id: Optional[int] = None
        if workflow is not None and workflow.state == "awaiting_operator":
            bin_id = workflow.bin_id
        else:
            placed = [unit for unit in await self.units(job, ["in_progress"]) if unit.stage == "placed"]
            if placed:
                bin_id = placed[0].bin_id
        if bin_id is None:
            raise ImportJobError(409, "no_tray", "No import bin is waiting to be returned")
        tray = await self.session.get(StorageBin, bin_id)
        if tray is None:
            raise ImportJobError(409, "tray_missing", "This bin no longer exists.")
        return tray

    async def begin_tray_return(self, job: ImportJob) -> Optional[StorageBinWorkflow]:
        """Close filling and open the tray's return with all known contents confirmed.

        Returns the prepared return workflow (in `returning_bin`) for the caller to move, or
        None when nothing has to move: crash recovery already put the tray back, or this
        return was already submitted. Internal motions are not deduplicated, so only a
        return this call prepared may be enqueued.
        """
        tray = await self.tray_for_return(job)
        workflow = await self._tray_workflow(job)
        reopen = workflow is not None and workflow.state == "awaiting_operator"
        if workflow is not None and reopen:
            await self.bins.close_batch(workflow)
        placed = [
            unit
            for unit in await self.units(job, ["in_progress"])
            if unit.stage == "placed" and unit.bin_id == tray.id
        ]
        await self.session.refresh(tray)
        if tray.status == "at_home":
            # The operator confirmed the tray back on its shelf in the crash dialog.
            for unit in placed:
                self._mark_loaded(unit)
            await self.session.commit()
            await self.settle(job)
            return None
        returning: Optional[StorageBinWorkflow] = None
        try:
            returning = await self.bins.begin_return(tray.barcode)
            if returning is None:
                raise BinWorkflowConflict("This bin no longer exists.")
            candidates = [int(i) for i in (returning.resolution or {}).get("candidate_item_ids", [])]
            prepared = await self.bins.prepare_return(returning, candidates)
        except BinWorkflowConflict as exc:
            # Leave the tray as it was: the batch keeps filling and no return stays open.
            if returning is not None and returning.state == "awaiting_operator":
                await self.bins.transition(returning, "cancelled", str(exc))
            if workflow is not None and reopen:
                await self.bins.transition(workflow, "awaiting_operator", "import bin return rejected")
            raise ImportJobError(409, "tray_return_rejected", str(exc)) from exc
        for unit in placed:
            unit.stage = "returning"
            unit.request_id = returning.id
            unit.error = None
        await self.session.commit()
        return returning if prepared else None

    async def apply_tray_return(self, returning: StorageBinWorkflow) -> None:
        """Mark units loaded once a return finished without a robot event (mock mode)."""
        for unit in (
            await self.session.scalars(
                select(ImportJobUnit).where(
                    ImportJobUnit.request_id == returning.id, ImportJobUnit.status == "in_progress"
                )
            )
        ).all():
            if returning.state == "completed":
                self._mark_loaded(unit)
            else:
                self._mark_return_failed(unit, returning.error)
        await self.session.commit()

    async def fail_tray_return(self, returning: StorageBinWorkflow, error: str) -> None:
        await self.bins.fail(returning, error)
        await self.apply_tray_return(returning)

    # ----------------------------------------------------------- robot events

    async def handle_command_result(self, request_id: Optional[str], payload: dict[str, Any]) -> Optional[ImportJob]:
        """Apply a terminal command result to the import unit or tray it belongs to."""
        if not request_id:
            return None
        units = (
            await self.session.scalars(
                select(ImportJobUnit).where(
                    ImportJobUnit.request_id == request_id, ImportJobUnit.status == "in_progress"
                )
            )
        ).all()
        succeeded = payload.get("message") == "success"
        job: Optional[ImportJob] = None
        for unit in units:
            job = await self.session.get(ImportJob, unit.job_id)
            if job is None:
                continue
            if unit.stage in ("measured", "storing"):
                if payload.get("command") != "store_item":
                    continue
                if succeeded:
                    self._mark_loaded(unit)
                else:
                    self._mark_store_failed(job, unit, payload.get("error"))
            elif unit.stage == "returning":
                workflow = await self.session.get(StorageBinWorkflow, request_id)
                if workflow is not None and workflow.state == "completed":
                    self._mark_loaded(unit)
                elif workflow is not None and workflow.state == "failed":
                    self._mark_return_failed(unit, workflow.error or payload.get("error"))
                elif succeeded:
                    # The bin workflow keeps its return reserved when inventory checks fail;
                    # the unit stays returning until that resolves.
                    continue
        if job is None:
            job = await self.session.scalar(
                select(ImportJob).where(
                    ImportJob.tray_workflow_id == request_id, ImportJob.state.in_(OPEN_STATES)
                )
            )
        if job is None:
            return None
        await self.session.commit()
        await self.settle(job)
        return job

    async def publish(self, job: ImportJob, publisher: Optional[EventPublisher] = None) -> None:
        target = publisher or event_publisher()
        if target is None:
            return
        step = await self.next_step(job)
        try:
            await target.publish_event(
                {
                    "kind": "import_job_updated",
                    "payload": {**await self.summary(job), "step": step["kind"] if step else None},
                }
            )
        except Exception as exc:
            logger.warning("Failed to publish import job update: %s", exc)


async def notify_command_result(
    request_id: Optional[str],
    payload: dict[str, Any],
    publisher: Optional[EventPublisher] = None,
) -> None:
    """Hook for both robot runtimes: update and announce the import a result belongs to."""
    if not request_id:
        return
    try:
        async with _lock:
            async with AsyncSessionLocal() as session:
                service = ImportJobService(session)
                job = await service.handle_command_result(request_id, payload)
                if job is not None:
                    await service.publish(job, publisher)
    except Exception as exc:
        logger.exception("Failed to apply robot result to import job: %s", exc)


def mutation_lock() -> asyncio.Lock:
    return _lock


__all__ = [
    "BATCH_OPERATION",
    "ImportJobError",
    "ImportJobService",
    "ImportRowInput",
    "MAX_UNITS",
    "event_publisher",
    "generate_barcode",
    "is_mock_mode",
    "mutation_lock",
    "notify_command_result",
]
